import time
import re
import threading
import win32gui
import win32process
import psutil
from pywinauto import Application
from pywinauto.keyboard import send_keys
from mcp_desktop_bridge.clipboard import ClipboardManager, ClipboardError

# 여러 요청이 클립보드 및 GUI 포커스를 동시에 훼손하지 않도록 보장하는 전역 락(Mutex)
gui_lock = threading.Lock()

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
                        "hwnd": hwnd, # 윈도우 직접 핸들 추가
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
    전체 실행 블록은 전역 Mutex 락에 의해 격리됩니다.
    """
    with gui_lock:
        apps_profiles = config.get("apps", {}) or {}
        app_profile = apps_profiles.get(app_name, {})
        default_config = config.get("default", {})

        input_offset_y = app_profile.get("input_offset_y", default_config.get("input_offset_y", -80))
        chat_offset_y = app_profile.get("chat_offset_y", default_config.get("chat_offset_y", -300))
        default_wait = app_profile.get("default_wait", default_config.get("default_wait", 10))

        max_wait = wait_seconds if wait_seconds is not None else default_wait

        submit_keys = app_profile.get("submit_keys", default_config.get("submit_keys", "{ENTER}"))

        # 1. 창 활성화
        dlg, window_title = find_and_focus_window(app_name, config)
        time.sleep(0.5)

        # 이전 선택 블록(Ctrl+A)을 해제하고 입력창 포커스를 초기화하기 위해 ESC 전송
        send_keys("{ESC}")
        time.sleep(0.3)

        rect = dlg.rectangle()

        # 2. 붙여넣기 전 기존 대화 내역 길이 백업 (초기 생각 시간 지연 감지용)
        chat_click_x, chat_click_y = get_offset_coords(rect, chat_offset_y)
        dlg.click_input(coords=(chat_click_x, chat_click_y))
        time.sleep(0.2)
        with ClipboardManager() as cb:
            cb.set_sentinel()
            send_keys("^a")
            time.sleep(0.3)
            send_keys("^c")
            time.sleep(0.3)
            initial_text = cb.get_text()
            if initial_text == "__SENTINEL_COPY_PENDING__":
                initial_text = ""

        # 다시 입력창으로 포커스 복귀 및 선택 블록 해제
        send_keys("{ESC}")
        time.sleep(0.3)

        # 3. 입력창 포커스 클릭 (설정된 경우에만 실행 - 현재는 null)
        if input_offset_y is not None and input_offset_y != 0:
            click_x, click_y = get_offset_coords(rect, input_offset_y)
            dlg.click_input(coords=(click_x, click_y))
            time.sleep(0.3)

        # 4. 클립보드 활용 복사 붙여넣기 (Ctrl+V) 고속 전송
        with ClipboardManager() as cb:
            cb.set_text(prompt)
            time.sleep(0.3)
            send_keys("^v")
            time.sleep(1.5)  # 대기 (붙여넣기 렌더링 시간 확보)
            send_keys(submit_keys)
            time.sleep(0.5)

        # 5. 실시간 안정화(답변 완료) 동적 대기 감지 루프
        print(f"[{window_title}] AI 답변 완료를 실시간 감지 중 (최대 {max_wait}초 대기)...")
        
        last_text = ""
        stable_count = 0
        start_time = time.time()
        
        # 최소 기대 길이 (기존 텍스트 + 새 질문 + 답변이 최소 30자 이상 작성이 시작되었는지 검증)
        min_expected_len = len(initial_text) + len(prompt) + 30
        
        # 대화창에 포커스를 주어 클립보드 복사 영역 선택 (루프 진입 전 한 번만 클릭하여 마우스 움직임 에러 및 깜빡임 방지)
        dlg.click_input(coords=(chat_click_x, chat_click_y))
        time.sleep(0.3)
        
        while time.time() - start_time < max_wait:
            time.sleep(4.0) # 4초 주기 폴링
            
            with ClipboardManager() as cb:
                cb.set_sentinel() # 센티널 설정
                send_keys("^a")
                time.sleep(0.3)
                send_keys("^c")
                time.sleep(0.3)
                current_text = cb.get_text()
            
            # 복사 명령이 윈도우 메시지 큐에서 유실된 경우 재시도
            if current_text == "__SENTINEL_COPY_PENDING__":
                print("Warning: 복사 응답이 지연되어 재시도합니다.")
                continue
            
            # 이전 폴링 텍스트와 비교
            if current_text and current_text == last_text:
                # 실제로 이전 대화 + 프롬프트 + 최소 답변 한 글자 이상 작성되어 최소 기대 길이를 넘었는지 확인
                if len(current_text) > min_expected_len:
                    stable_count += 1
                    if stable_count >= 2: # 8초 동안 글자 수 변화 감지 없음 -> 완료
                        print(f"[{window_title}] 실시간 완료 감지! 답변 캡처에 성공했습니다.")
                        break
            else:
                stable_count = 0
                last_text = current_text
                
        # 5. 최종 데이터 검증 복사
        time.sleep(0.5)
        dlg.click_input(coords=(chat_click_x, chat_click_y))
        time.sleep(0.2)
        with ClipboardManager() as cb:
            cb.set_sentinel()
            send_keys("^a")
            time.sleep(0.3)
            send_keys("^c")
            time.sleep(0.3)
            final_text = cb.get_text()
            
        if final_text == "__SENTINEL_COPY_PENDING__":
            # 최종 복사가 일시 실패한 경우 루프에서의 안정 텍스트 백업본 복귀
            final_text = last_text if last_text else "Error: 최종 텍스트 복사에 실패했습니다."
            
        return final_text

def send_keys_to_window(app_name: str, keys: str, config: dict) -> str:
    """
    대상 앱 창에 단축키 또는 임의의 키입력을 전송합니다. 전역 락에 의해 동기적으로 실행됩니다.
    """
    with gui_lock:
        dlg, window_title = find_and_focus_window(app_name, config)
        time.sleep(0.3)
        send_keys(keys, with_spaces=True)
        return f"'{window_title}' 창에 키 입력 '{keys}'을(를) 성공적으로 전송했습니다."
