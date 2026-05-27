import time
import sys
import re
import uuid
import win32gui
import win32process
import win32event
import win32api
import win32con
import psutil
from pywinauto import Application
from pywinauto.keyboard import send_keys
from mcp_desktop_bridge.clipboard import ClipboardManager, ClipboardError

MUTEX_NAME = "Global\\MCPWindowsAutomationMutex"

class NamedMutex:
    """
    Windows 시스템 전역 Named Mutex를 활용하여 프로세스/스레드 간 동기화를 지원합니다.
    """
    def __init__(self, name: str, timeout_ms: int = 15000):
        self.name = name
        self.timeout_ms = timeout_ms
        self.handle = None

    def __enter__(self):
        try:
            self.handle = win32event.CreateMutex(None, False, self.name)
        except Exception as e:
            # Global\\ 네임스페이스 생성이 관리자 권한 등의 문제로 실패할 경우 Local\\ 로 대체
            if "Global\\" in self.name:
                fallback_name = self.name.replace("Global\\", "Local\\")
                try:
                    self.handle = win32event.CreateMutex(None, False, fallback_name)
                except Exception:
                    raise e
            else:
                raise e

        try:
            res = win32event.WaitForSingleObject(self.handle, self.timeout_ms)
            if res not in (win32con.WAIT_OBJECT_0, win32con.WAIT_ABANDONED):
                raise TimeoutError(f"시스템 전역 락({self.name}) 획득에 실패했습니다. (res={res})")
        except Exception as e:
            if self.handle:
                win32api.CloseHandle(self.handle)
                self.handle = None
            raise e
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.handle:
            try:
                win32event.ReleaseMutex(self.handle)
            finally:
                win32api.CloseHandle(self.handle)
                self.handle = None


def list_windows() -> list[dict]:
    """
    현재 윈도우 OS에서 실행 중인 보이는(Visible) GUI 창 타이틀 목록을 조회합니다.
    창 핸들(hwnd)과 함께 프로세스 정보를 함께 수집합니다.
    """
    windows = []

    def win_enum_callback(hwnd, ctx):
        if win32gui.IsWindowVisible(hwnd):
            title = win32gui.GetWindowText(hwnd)
            if title:
                # 프로세스 정보 가져오기
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                try:
                    process = psutil.Process(pid)
                    proc_name = process.name()
                except Exception:
                    proc_name = "unknown"

                proc_lower = proc_name.lower()
                if proc_lower not in ["explorer.exe", "shellexperiencehost.exe", "searchhost.exe", "taskmgr.exe"]:
                    windows.append({
                        "hwnd": hwnd,
                        "pid": pid,
                        "process_name": proc_name,
                        "window_title": title
                    })

    win32gui.EnumWindows(win_enum_callback, None)
    windows.sort(key=lambda x: x["window_title"].lower())
    return windows


def find_and_focus_window(app_name: str, config: dict):
    """
    애플리케이션 이름 또는 config.yaml 설정을 기반으로 창 핸들을 직접 매칭해 최전면으로 띄웁니다.
    """
    title_regex = f".*{re.escape(app_name)}.*"
    apps_profiles = config.get("apps", {}) or {}
    
    if app_name in apps_profiles:
        title_regex = apps_profiles[app_name].get("window_title_re", title_regex)

    active_wins = list_windows()
    target_win = None
    
    # 정규식 매칭
    compiled_regex = re.compile(title_regex, re.IGNORECASE)
    for win in active_wins:
        if compiled_regex.match(win["window_title"]):
            target_win = win
            break

    # 단순 포함 매칭
    if not target_win:
        for win in active_wins:
            if app_name.lower() in win["window_title"].lower() or app_name.lower() in win["process_name"].lower():
                target_win = win
                break

    if not target_win:
        raise ValueError(
            f"'{app_name}' 에 해당하는 활성 창을 찾을 수 없습니다. "
            f"프로그램이 실행 중인지 확인해 주세요."
        )

    # UIA를 활용해 발견된 고유 HWND 핸들로 직접 연결 (제목 불일치 오류 방지)
    hwnd = target_win["hwnd"]
    app = Application(backend="uia").connect(handle=hwnd, visible_only=True)
    dlg = app.window(handle=hwnd)

    if dlg.is_minimized():
        dlg.restore()
        
    dlg.set_focus()
    return dlg, target_win["window_title"]


def get_offset_coords(rect, offset_y: int) -> tuple[int, int]:
    """
    윈도우 영역과 Y 오프셋을 기준으로 X, Y 상대 좌표를 계산합니다.
    """
    width = rect.width()
    height = rect.height()
    
    click_x = width // 2
    if offset_y < 0:
        click_y = height + offset_y
    else:
        click_y = offset_y
        
    return click_x, click_y


def send_query_to_window(app_name: str, prompt: str, config: dict, wait_seconds: int = None) -> str:
    """
    대상 애플리케이션 창에 텍스트 프롬프트를 보내고, 답변 완료 상태를 감지하여 응답을 긁어옵니다.
    전체 실행 블록은 시스템 전역 Mutex 락 및 ClipboardManager 백업 안전 가드에 의해 보호됩니다.
    """
    apps_profiles = config.get("apps", {}) or {}
    app_profile = apps_profiles.get(app_name, {})
    default_config = config.get("default", {})

    input_offset_y = app_profile.get("input_offset_y", default_config.get("input_offset_y", -80))
    chat_offset_y = app_profile.get("chat_offset_y", default_config.get("chat_offset_y", -300))
    default_wait = app_profile.get("default_wait", default_config.get("default_wait", 10))
    submit_keys = app_profile.get("submit_keys", default_config.get("submit_keys", "{ENTER}"))
    keystroke_delay = app_profile.get("keystroke_delay", default_config.get("keystroke_delay", 0.05))
    use_uuid_marker = app_profile.get("use_uuid_marker", default_config.get("use_uuid_marker", False))

    max_wait = wait_seconds if wait_seconds is not None else default_wait

    # 대기 시간(max_wait) 검증
    if not isinstance(max_wait, (int, float)) or max_wait <= 0:
        raise ValueError(f"최대 대기 시간(wait_seconds)은 0보다 큰 숫자여야 합니다. (입력값: {max_wait})")
    if max_wait > 300:
        print(f"Warning: 요청된 대기 시간({max_wait}초)이 제한을 초과하여 300초로 조정합니다.", file=sys.stderr)
        max_wait = 300

    request_id = str(uuid.uuid4())
    marker_str = f"[MCP_END_OF_PROMPT: {request_id}]"

    # UUID marker 적용 시 프롬프트 후미에 마커 삽입
    if use_uuid_marker:
        marked_prompt = f"{prompt}\n{marker_str}\n"
    else:
        marked_prompt = prompt

    with NamedMutex(MUTEX_NAME):
        # 원본 클립보드 상태 백업 (백업 실패 시 자동화 즉시 중단)
        with ClipboardManager(require_backup=True) as cb:
            # 1. 창 활성화
            dlg, window_title = find_and_focus_window(app_name, config)
            time.sleep(0.5)

            # 이전 선택 해제 및 포커스 초기화를 위해 ESC 전송
            send_keys("{ESC}", pause=keystroke_delay)
            time.sleep(0.3)

            rect = dlg.rectangle()
            chat_click_x, chat_click_y = get_offset_coords(rect, chat_offset_y)

            # 2. 붙여넣기 전 기존 대화 내역 길이 백업 (마우스 움직임 최소화를 위해 루프 밖에서 1회 포커스)
            dlg.click_input(coords=(chat_click_x, chat_click_y))
            time.sleep(0.2)

            cb.set_sentinel()
            send_keys("^a", pause=keystroke_delay)
            time.sleep(0.3)
            send_keys("^c", pause=keystroke_delay)
            time.sleep(0.3)
            initial_text = cb.get_text()
            if initial_text == "__SENTINEL_COPY_PENDING__":
                initial_text = ""

            # 다시 입력창 포커스로 복귀 및 선택 블록 해제
            send_keys("{ESC}", pause=keystroke_delay)
            time.sleep(0.3)

            # 3. 입력창 포커스 클릭 (설정된 경우에만 실행)
            if input_offset_y is not None and input_offset_y != 0:
                click_x, click_y = get_offset_coords(rect, input_offset_y)
                dlg.click_input(coords=(click_x, click_y))
                time.sleep(0.3)

            # 4. 클립보드 활용하여 프롬프트 쓰기 및 붙여넣기 전송
            cb.set_text(marked_prompt)
            time.sleep(0.3)
            send_keys("^v", pause=keystroke_delay)
            time.sleep(1.5)  # 붙여넣기 렌더링 시간 충분히 확보
            send_keys(submit_keys, pause=keystroke_delay)
            time.sleep(0.5)

            # 5. 실시간 답변 완료 감지 루프
            print(f"[{window_title}] AI 답변 완료를 실시간 감지 중 (최대 {max_wait}초 대기, UUID 마커: {use_uuid_marker})...", file=sys.stderr)

            last_stable_text = ""
            stable_count = 0
            start_time = time.time()
            poll_interval = 2.0

            # 대화창에 포커스를 주어 클립보드 복사 영역 선택 (폴링 중 마우스 움직임 방지)
            dlg.click_input(coords=(chat_click_x, chat_click_y))
            time.sleep(0.3)

            while True:
                elapsed = time.time() - start_time
                remaining = max_wait - elapsed
                if remaining <= 0:
                    break

                time.sleep(min(poll_interval, remaining))

                cb.set_sentinel()
                send_keys("^a", pause=keystroke_delay)
                time.sleep(0.3)
                send_keys("^c", pause=keystroke_delay)
                time.sleep(0.3)
                current_text = cb.get_text()

                if current_text == "__SENTINEL_COPY_PENDING__":
                    print("Warning: 복사 응답이 지연되어 재시도합니다.", file=sys.stderr)
                    continue

                if use_uuid_marker:
                    # 마커 문자열이 대화 내역에 나타났는지 확인
                    if marker_str not in current_text:
                        stable_count = 0
                        continue

                    # 마커 이후 생성된 텍스트 추출
                    parts = current_text.split(marker_str)
                    suffix = parts[-1]

                    # AI가 응답을 생성하기 시작할 때까지 대기 (최소 2자 확보)
                    if len(suffix.strip()) < 2:
                        stable_count = 0
                        continue

                    if suffix == last_stable_text:
                        stable_count += 1
                        if stable_count >= 2:  # 4초(2초 * 2회) 동안 변화가 없는 경우 안정화 판단
                            print(f"[{window_title}] 실시간 완료 감지! (텍스트 안정화)", file=sys.stderr)
                            break
                    else:
                        stable_count = 0
                        last_stable_text = suffix
                else:
                    # 마커 미사용 시 단순 텍스트 길이 및 변화 감지
                    min_expected_len = len(initial_text) + len(prompt) - 5
                    if len(current_text) < min_expected_len:
                        stable_count = 0
                        continue

                    if current_text == last_stable_text:
                        stable_count += 1
                        if stable_count >= 2:
                            print(f"[{window_title}] 실시간 완료 감지! (텍스트 안정화)", file=sys.stderr)
                            break
                    else:
                        stable_count = 0
                        last_stable_text = current_text

            # 6. 최종 대화 데이터 검증 및 복사
            time.sleep(0.5)
            dlg.click_input(coords=(chat_click_x, chat_click_y))
            time.sleep(0.2)
            cb.set_sentinel()
            send_keys("^a", pause=keystroke_delay)
            time.sleep(0.3)
            send_keys("^c", pause=keystroke_delay)
            time.sleep(0.3)
            final_text = cb.get_text()

            if final_text == "__SENTINEL_COPY_PENDING__":
                final_text = last_stable_text if last_stable_text else "Error: 최종 텍스트 복사에 실패했습니다."

            # 최종 텍스트에서 마커 뒤의 AI 응답 부분만 슬라이싱하여 반환
            if use_uuid_marker and marker_str in final_text:
                final_text = final_text.split(marker_str)[-1].strip()

            return final_text


def send_keys_to_window(app_name: str, keys: str, config: dict) -> str:
    """
    대상 앱 창에 단축키 또는 임의의 키 이벤트를 전송합니다. 전역 락에 의해 동기적으로 실행됩니다.
    """
    apps_profiles = config.get("apps", {}) or {}
    app_profile = apps_profiles.get(app_name, {})
    default_config = config.get("default", {})
    keystroke_delay = app_profile.get("keystroke_delay", default_config.get("keystroke_delay", 0.05))

    with NamedMutex(MUTEX_NAME):
        dlg, window_title = find_and_focus_window(app_name, config)
        time.sleep(0.3)
        send_keys(keys, with_spaces=True, pause=keystroke_delay)
        return f"'{window_title}' 창에 키 입력 '{keys}'을(를) 성공적으로 전송했습니다."
