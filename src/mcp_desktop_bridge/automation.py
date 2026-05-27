import time
import re
import win32gui
import win32process
import psutil
from pywinauto import Application
from pywinauto.keyboard import send_keys
from mcp_desktop_bridge.clipboard import ClipboardManager

def list_windows() -> list[dict]:
    """
    현재 윈도우 OS에서 실행 중인 보이는(Visible) GUI 창 타이틀 목록을 조회합니다.
    시스템 프로세스 및 빈 타이틀을 필터링하여 반환합니다.
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

                # 시스템 관리 프로세스 필터링
                proc_lower = proc_name.lower()
                if proc_lower not in ["explorer.exe", "shellexperiencehost.exe", "searchhost.exe", "taskmgr.exe"]:
                    windows.append({
                        "pid": pid,
                        "process_name": proc_name,
                        "window_title": title
                    })

    win32gui.EnumWindows(win_enum_callback, None)
    # 타이틀 기준으로 정렬
    windows.sort(key=lambda x: x["window_title"].lower())
    return windows

def find_and_focus_window(app_name: str, config: dict):
    """
    애플리케이션 이름 또는 config.yaml 설정을 기반으로 창을 찾아 활성화합니다.
    """
    # 1. 대상 창 이름 규칙 로드
    title_regex = f".*{re.escape(app_name)}.*"
    apps_profiles = config.get("apps", {}) or {}
    
    if app_name in apps_profiles:
        title_regex = apps_profiles[app_name].get("window_title_re", title_regex)

    # 2. 실행 중인 창 목록 검색
    active_wins = list_windows()
    target_win = None
    
    # 2-1. 정규식 매칭 시도
    compiled_regex = re.compile(title_regex, re.IGNORECASE)
    for win in active_wins:
        if compiled_regex.match(win["window_title"]):
            target_win = win
            break

    # 2-2. 단순 문자열 포함 여부 매칭 시도
    if not target_win:
        for win in active_wins:
            if app_name.lower() in win["window_title"].lower() or app_name.lower() in win["process_name"].lower():
                target_win = win
                break

    if not target_win:
        raise ValueError(
            f"'{app_name}' 에 해당하는 활성 창을 찾을 수 없습니다. "
            f"앱이 백그라운드가 아닌 화면에 켜져 있는지 확인해 주세요."
        )

    # 3. 창 제어 연결 및 최전면 활성화
    app = Application(backend="uia").connect(process=target_win["pid"], visible_only=True)
    
    # 해당 PID를 가진 최상위 활성 윈도우 핸들 획득
    hwnd = win32gui.FindWindow(None, target_win["window_title"])
    if not hwnd:
        # 윈도우 타이틀이 약간 다를 경우를 대비하여 UIA 탑 윈도우 조회
        dlg = app.top_window()
    else:
        dlg = app.window(handle=hwnd)

    # 최소화 상태면 복원
    if dlg.is_minimized():
        dlg.restore()
        
    dlg.set_focus()
    return dlg, target_win["window_title"]

def get_offset_coords(rect, offset_y: int) -> tuple[int, int]:
    """
    윈도우 영역(rect)과 Y 오프셋을 기준으로 클릭할 상대적 X, Y 좌표를 계산합니다.
    Y 오프셋이 음수이면 하단 기준, 양수이면 상단 기준입니다.
    """
    width = rect.width()
    height = rect.height()
    
    click_x = width // 2
    if offset_y < 0:
        # 하단 기준 (예: -80 -> 하단 끝에서 80픽셀 위)
        click_y = height + offset_y
    else:
        # 상단 기준 (예: 150 -> 상단 끝에서 150픽셀 아래)
        click_y = offset_y
        
    return click_x, click_y

def send_query_to_window(app_name: str, prompt: str, config: dict, wait_seconds: int = None) -> str:
    """
    대상 애플리케이션 창에 텍스트 프롬프트를 보내고 응답을 긁어옵니다.
    """
    # 1. 프로필 정보 조회
    apps_profiles = config.get("apps", {}) or {}
    app_profile = apps_profiles.get(app_name, {})
    default_config = config.get("default", {})

    input_offset_y = app_profile.get("input_offset_y", default_config.get("input_offset_y", -80))
    chat_offset_y = app_profile.get("chat_offset_y", default_config.get("chat_offset_y", -300))
    default_wait = app_profile.get("default_wait", default_config.get("default_wait", 10))
    keystroke_delay = default_config.get("keystroke_delay", 0.01)

    wait_time = wait_seconds if wait_seconds is not None else default_wait

    # 2. 창 활성화
    dlg, window_title = find_and_focus_window(app_name, config)
    time.sleep(0.5)

    rect = dlg.rectangle()

    # 3. 입력창 포커스 클릭
    click_x, click_y = get_offset_coords(rect, input_offset_y)
    dlg.click_input(coords=(click_x, click_y))
    time.sleep(0.3)

    # 4. 클립보드를 이용해 프롬프트를 복사 붙여넣기(Ctrl+V)로 고속 입력
    # (일반 키 전송 시 발생하는 괄호, 특수 문자 등의 오작동 및 타이핑 딜레이를 완벽 방지)
    with ClipboardManager() as cb:
        cb.set_text(prompt)
        time.sleep(0.3)
        send_keys("^v")  # 붙여넣기 키 전송
        time.sleep(1.5)  # 중요: 앱이 클립보드를 읽어 실제로 붙여넣기를 처리할 때까지 충분히 대기 (레이스 컨디션 방지)
        send_keys("{ENTER}")  # 전송 키 전송
        time.sleep(0.5)  # 엔터 키가 앱에 입력될 때까지 대기
    time.sleep(0.5)

    # 5. 답변 대기
    print(f"[{window_title}] {wait_time}초 동안 답변 생성을 기다리는 중...")
    time.sleep(wait_time)

    # 6. 채팅 기록 클릭하여 포커스 후 복사
    chat_click_x, chat_click_y = get_offset_coords(rect, chat_offset_y)
    dlg.click_input(coords=(chat_click_x, chat_click_y))
    time.sleep(0.3)

    # 클립보드를 안전하게 복원하면서 복사 실행
    with ClipboardManager() as cb:
        send_keys("^a") # 전체 선택
        time.sleep(0.3)
        send_keys("^c") # 복사
        time.sleep(0.3)
        copied_text = cb.get_text()

    return copied_text

def send_keys_to_window(app_name: str, keys: str, config: dict) -> str:
    """
    대상 앱 창을 열고 임의의 단축키/키 입력을 전송합니다.
    """
    dlg, window_title = find_and_focus_window(app_name, config)
    time.sleep(0.3)
    
    send_keys(keys, with_spaces=True)
    return f"'{window_title}' 창에 키 입력 '{keys}'을(를) 성공적으로 전송했습니다."
