import time
import sys
import re
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
                    self.name = fallback_name  # 내부 상태에 실제 생성된 네임스페이스 이름 반영
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
    애플리케이션 이름 또는 config.yaml 설정을 기반으로 창 핸들을 직접 매칭해 최전면으로 띄우고 포커스 상태를 확인합니다.
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

    # Foreground Focus 획득 여부 검증 (최대 1초 대기)
    focused = False
    for _ in range(10):
        if win32gui.GetForegroundWindow() == hwnd:
            focused = True
            break
        time.sleep(0.1)

    if not focused:
        current_fg = win32gui.GetForegroundWindow()
        raise RuntimeError(
            f"윈도우 포커스 전환에 실패했습니다. (대상 HWND: {hwnd}, 현재 Foreground HWND: {current_fg}).\n"
            f"원격 데스크톱 세션 차단, UAC 세큐어 데스크톱, 혹은 다른 관리자 권한 앱의 포커스 잠금 때문일 수 있습니다."
        )

    return dlg, target_win["window_title"]


def get_offset_coords(rect, offset_y: int) -> tuple[int, int]:
    """
    윈도우 영역과 Y 오프셋을 기준으로 X, Y 상대 좌표를 계산합니다.
    좌표 클릭 실수 및 창 이탈 방지를 위해 윈도우 한계 영역 내로 Clamp 처리합니다.
    """
    width = rect.width()
    height = rect.height()
    
    click_x = width // 2
    if offset_y < 0:
        click_y = height + offset_y
    else:
        click_y = offset_y
        
    # 창 경계를 벗어나서 클릭 시 다른 창이 선택되는 문제를 막기 위한 Clamp 처리 (10px 패딩)
    click_x = max(10, min(click_x, width - 10))
    click_y = max(10, min(click_y, height - 10))
        
    return click_x, click_y


def try_focus_input_uia(dlg) -> bool:
    """UIA 트리 구조를 탐색하여 Edit/Document 타입의 입력 컨트롤을 찾아 직접 포커스를 부여합니다."""
    try:
        for ctrl_type in ["Edit", "Document"]:
            try:
                ctrl = dlg.child_window(control_type=ctrl_type)
                if ctrl.exists() and ctrl.is_visible():
                    ctrl.set_focus()
                    return True
            except Exception:
                pass
    except Exception:
        pass
    return False


def extract_response(current_text: str, initial_text: str, prompt: str) -> str:
    """대화 내역 텍스트 전체에서 프롬프트 이후에 생성된 AI 응답 텍스트 영역만 정밀 분할해 반환합니다."""
    # 1. 초기 텍스트 snapshot 분할을 통해 이번 대화 턴의 영역 추출
    if initial_text and initial_text in current_text:
        this_turn = current_text.split(initial_text)[-1]
    else:
        this_turn = current_text

    # 2. 프롬프트 원문을 기준으로 응답 추출 시도
    if prompt in this_turn:
        return this_turn.split(prompt)[-1].strip()

    # 3. 프롬프트가 UI 형식에 맞게 개행이 제거되거나 일부 잘린 경우 대비하여 라인 매칭 시도
    # 빈 줄을 제외하고 의미 있는 라인만 추출
    lines = [line.strip() for line in prompt.split("\n") if len(line.strip()) > 10]
    for line in reversed(lines):
        if line in this_turn:
            return this_turn.split(line)[-1].strip()

    # 4. 첫 번째 유의미한 줄 기준 매칭 시도
    first_line = prompt.split("\n")[0].strip()
    if len(first_line) > 5 and first_line in this_turn:
        return this_turn.split(first_line)[-1].strip()

    # 5. 모든 분할 규칙이 작동하지 않을 경우 대화 턴 전체 반환
    return this_turn.strip()


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
    wait_for_generation = app_profile.get("wait_for_generation", default_config.get("wait_for_generation", False))

    max_wait = wait_seconds if wait_seconds is not None else default_wait

    # 대기 시간(max_wait) 검증
    if not isinstance(max_wait, (int, float)) or max_wait <= 0:
        raise ValueError(f"최대 대기 시간(wait_seconds)은 0보다 큰 숫자여야 합니다. (입력값: {max_wait})")
    if max_wait > 300:
        print(f"Warning: 요청된 대기 시간({max_wait}초)이 제한을 초과하여 300초로 조정합니다.", file=sys.stderr)
        max_wait = 300

    with NamedMutex(MUTEX_NAME):
        # 원본 클립보드 상태 백업 (백업 실패 또는 백업 불가능한 불안정 포맷 감지 시 자동화 즉시 중단)
        with ClipboardManager(require_backup=True) as cb:
            # 1. 창 활성화 및 검증
            dlg, window_title = find_and_focus_window(app_name, config)
            time.sleep(0.5)

            # 이전 선택 해제 및 포커스 초기화를 위해 ESC 전송
            send_keys("{ESC}", pause=keystroke_delay)
            time.sleep(0.3)

            rect = dlg.rectangle()
            chat_click_x, chat_click_y = get_offset_coords(rect, chat_offset_y)

            # 2. 붙여넣기 전 기존 대화 내역 snapshot 가져오기 (마우스 움직임 최소화를 위해 루프 밖에서 1회 포커스)
            dlg.click_input(coords=(chat_click_x, chat_click_y))
            time.sleep(0.2)

            send_keys("^a", pause=keystroke_delay)
            time.sleep(0.2)
            initial_text = cb.copy_from_focused_app(keystroke_delay=keystroke_delay)
            if initial_text == "__SENTINEL_COPY_PENDING__":
                initial_text = ""

            # 다시 포커스 초기화
            send_keys("{ESC}", pause=keystroke_delay)
            time.sleep(0.3)

            # 3. 입력창 포커스 (UIA 우선 시도 후 실패 시 좌표 클릭)
            if not try_focus_input_uia(dlg):
                if input_offset_y is not None and input_offset_y != 0:
                    click_x, click_y = get_offset_coords(rect, input_offset_y)
                    dlg.click_input(coords=(click_x, click_y))
                    time.sleep(0.3)

            # 4. 클립보드를 활용하여 프롬프트 쓰기 및 붙여넣기 전송
            cb.set_text(prompt)
            time.sleep(0.3)
            send_keys("^v", pause=keystroke_delay)
            time.sleep(1.5)  # 붙여넣기 렌더링 시간 확보
            send_keys(submit_keys, pause=keystroke_delay)
            time.sleep(0.5)

            # 대화창에 포커스를 주어 클립보드 복사 준비
            dlg.click_input(coords=(chat_click_x, chat_click_y))
            time.sleep(0.3)

            # 제출 직후의 대화 상태 복사 (텍스트 증가 비교를 위한 기준점)
            post_submit_text = cb.copy_from_focused_app(keystroke_delay=keystroke_delay)
            if post_submit_text == "__SENTINEL_COPY_PENDING__":
                post_submit_text = initial_text

            # 5. 실시간 답변 완료 감지 루프
            print(f"[{window_title}] AI 답변 완료를 실시간 감지 중 (최대 {max_wait}초 대기, 대기 전략: {wait_for_generation})...", file=sys.stderr)

            last_stable_text = ""
            stable_count = 0
            start_time = time.time()
            poll_interval = 2.0

            while True:
                elapsed = time.time() - start_time
                remaining = max_wait - elapsed
                if remaining <= 0:
                    break

                time.sleep(min(poll_interval, remaining))

                # 안전하게 시퀀스를 업데이트하며 텍스트 긁기
                current_text = cb.copy_from_focused_app(keystroke_delay=keystroke_delay)

                if current_text == "__SENTINEL_COPY_PENDING__":
                    print("Warning: 복사 응답이 지연되어 재시도합니다.", file=sys.stderr)
                    continue

                # AI 답변 생성을 대기하는 모드일 경우, 복사된 내용이 제출 직후 기준점보다 유의미하게 늘어났는지 검증
                if wait_for_generation:
                    if len(current_text.strip()) <= len(post_submit_text.strip()) + 2:
                        stable_count = 0
                        continue

                # 응답 영역 추출
                suffix = extract_response(current_text, initial_text, prompt)

                if suffix == last_stable_text:
                    stable_count += 1
                    if stable_count >= 2:  # 4초(2초 * 2회) 동안 변화가 없는 경우 안정화 판단
                        print(f"[{window_title}] 실시간 완료 감지! (텍스트 안정화)", file=sys.stderr)
                        break
                else:
                    stable_count = 0
                    last_stable_text = suffix

            # 6. 최종 대화 데이터 검증 및 복사
            time.sleep(0.5)
            dlg.click_input(coords=(chat_click_x, chat_click_y))
            time.sleep(0.2)
            
            send_keys("^a", pause=keystroke_delay)
            time.sleep(0.2)
            final_text = cb.copy_from_focused_app(keystroke_delay=keystroke_delay)

            if final_text == "__SENTINEL_COPY_PENDING__":
                final_text = last_stable_text if last_stable_text else "Error: 최종 텍스트 복사에 실패했습니다."

            # 생성 대기 모드인 경우 AI 응답 영역만 발라내어 반환
            if wait_for_generation:
                final_text = extract_response(final_text, initial_text, prompt)

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
