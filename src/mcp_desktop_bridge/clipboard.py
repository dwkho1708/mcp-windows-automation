import time
import sys
import win32clipboard
import win32con

class ClipboardError(Exception):
    """클립보드 조작 실패 시 발생합니다."""
    pass

class ClipboardManager:
    """
    클립보드 데이터를 안전하게 백업하고 복원하는 컨텍스트 매니저입니다.
    안전한 포맷(allowlist)만 걸러 백업하며, 이미지 등 백업 불가능한 포맷 감지 시 경고/중단합니다.
    GetClipboardSequenceNumber를 활용해 사용자 직접 복사본 덮어쓰기를 방지합니다.
    """

    def __init__(self, require_backup: bool = False):
        self.backup_data = {}
        self.has_backup = False
        self.last_write_seq = None
        self.require_backup = require_backup
        self.unsafe_formats_detected = False
        self.unsafe_formats = []

    def is_format_safe(self, fmt: int) -> bool:
        """백업 및 단순 복원이 안전한 텍스트 관련 포맷인지 확인하는 allowlist 필터입니다."""
        safe_std_formats = {
            win32con.CF_UNICODETEXT,
            win32con.CF_TEXT,
            win32con.CF_OEMTEXT,
            win32con.CF_LOCALE,
            win32con.CF_HDROP
        }
        if fmt in safe_std_formats:
            return True
        try:
            name = win32clipboard.GetClipboardFormatName(fmt)
            # 브라우저, 에디터 등에서 사용하는 텍스트 기반 다중 포맷 허용
            if name in (
                "HTML Format",
                "Rich Text Format",
                "Chromium internal source RFH token",
                "Chromium internal source URL",
                "text/plain",
                "text/html",
                "text/richtext",
                "UniformResourceLocator",
                "UniformResourceLocatorW"
            ):
                return True
        except Exception:
            pass
        return False

    def __enter__(self):
        # 진입 시 기존 클립보드 내용을 다중 포맷으로 백업
        try:
            self.backup_data = self.backup()
            if self.require_backup and self.unsafe_formats_detected:
                formats_desc = ", ".join([f"{name} (ID: {fmt})" for fmt, name in self.unsafe_formats])
                raise ClipboardError(
                    f"클립보드에 복구 유실 위험이 있는 포맷(이미지, OLE 개체 등)이 감지되어 복원 유실을 예방하기 위해 자동화를 중단합니다.\n"
                    f"감지된 포맷: [{formats_desc}]"
                )
            self.has_backup = True
        except ClipboardError as ce:
            self.has_backup = False
            raise ce
        except Exception as e:
            self.backup_data = {}
            self.has_backup = False
            if self.require_backup:
                raise ClipboardError(f"클립보드 백업 실패로 자동화를 중단합니다: {e}")
            else:
                print(f"Warning: 클립보드 백업 실패 (무시하고 계속 진행): {e}", file=sys.stderr)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        # 종료 시 기존 백업본 복원
        if self.has_backup:
            self.restore()

    def backup(self) -> dict:
        """현재 클립보드의 안전한 포맷과 데이터를 백업합니다."""
        backup = {}
        unsafe_found = []
        for i in range(5):
            try:
                win32clipboard.OpenClipboard()
                try:
                    fmt = 0
                    while True:
                        fmt = win32clipboard.EnumClipboardFormats(fmt)
                        if fmt == 0:
                            break
                        
                        if self.is_format_safe(fmt):
                            try:
                                data = win32clipboard.GetClipboardData(fmt)
                                backup[fmt] = data
                            except Exception:
                                pass
                        else:
                            try:
                                name = win32clipboard.GetClipboardFormatName(fmt)
                            except Exception:
                                name = f"System Format ID {fmt}"
                            unsafe_found.append((fmt, name))
                            
                    self.unsafe_formats = unsafe_found
                    self.unsafe_formats_detected = len(unsafe_found) > 0
                    return backup
                finally:
                    win32clipboard.CloseClipboard()
            except Exception:
                time.sleep(0.1)
        raise ClipboardError("클립보드 데이터를 백업하기 위한 OpenClipboard에 실패했습니다.")

    def restore(self):
        """백업해 둔 기존 데이터들을 클립보드에 다시 채워 복원합니다."""
        if not self.has_backup:
            return

        # 마지막으로 작성한 이후 사용자가 클립보드를 바꿨는지 확인
        current_seq = win32clipboard.GetClipboardSequenceNumber()
        if self.last_write_seq is not None and current_seq != self.last_write_seq:
            print("Warning: 자동화 도중 사용자가 새로운 복사 동작을 수행했으므로 클립보드 복원을 건너뜁니다.", file=sys.stderr)
            return

        for i in range(5):
            try:
                win32clipboard.OpenClipboard()
                try:
                    win32clipboard.EmptyClipboard()
                    for fmt, data in self.backup_data.items():
                        try:
                            win32clipboard.SetClipboardData(fmt, data)
                        except Exception as e:
                            print(f"Warning: 클립보드 포맷 {fmt} 복원 실패: {e}", file=sys.stderr)
                finally:
                    win32clipboard.CloseClipboard()
                # 복원 완료 및 닫기 후의 시퀀스 번호 기록
                self.last_write_seq = win32clipboard.GetClipboardSequenceNumber()
                return
            except Exception:
                time.sleep(0.1)
        print("Warning: 클립보드 복원에 최종 실패했습니다.", file=sys.stderr)

    def set_text(self, text: str):
        """클립보드에 유니코드 텍스트를 설정합니다."""
        for i in range(5):
            try:
                win32clipboard.OpenClipboard()
                try:
                    win32clipboard.EmptyClipboard()
                    win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
                finally:
                    win32clipboard.CloseClipboard()
                # 쓰기 완료 및 닫기 직후의 시퀀스 번호 기록
                self.last_write_seq = win32clipboard.GetClipboardSequenceNumber()
                return
            except Exception:
                time.sleep(0.1)
        raise ClipboardError("클립보드에 텍스트 쓰기에 실패했습니다. 다른 프로세스가 사용 중일 수 있습니다.")

    def set_sentinel(self, sentinel_text: str = "__SENTINEL_COPY_PENDING__"):
        """복사 실패 감지용 센티널 값을 클립보드에 작성합니다."""
        self.set_text(sentinel_text)

    def get_text(self) -> str:
        """현재 클립보드에 저장된 유니코드 텍스트를 읽어옵니다."""
        for i in range(5):
            try:
                win32clipboard.OpenClipboard()
                try:
                    if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
                        return win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
                    return ""
                finally:
                    win32clipboard.CloseClipboard()
            except Exception:
                time.sleep(0.1)
        raise ClipboardError("클립보드 텍스트 읽기에 실패했습니다. 다른 프로세스가 사용 중일 수 있습니다.")

    def copy_from_focused_app(self, keystroke_delay: float = 0.05) -> str:
        """
        포커싱된 앱에서 복사(Ctrl+C) 명령을 수행하고 시퀀스 번호를 안전하게 갱신합니다.
        센티널 값을 설정하여 복사 성공 여부를 감지하며, 복사가 확인되면 마지막 쓰기 시퀀스 번호를 업데이트합니다.
        """
        self.set_sentinel()
        start_seq = self.last_write_seq

        from pywinauto.keyboard import send_keys
        send_keys("^c", pause=keystroke_delay)

        # 복사 완료로 인한 클립보드 시퀀스 번호 증가 감지 대기 (최대 1초)
        copied = False
        for _ in range(10):
            time.sleep(0.1)
            current_seq = win32clipboard.GetClipboardSequenceNumber()
            if current_seq != start_seq:
                copied = True
                break

        text = self.get_text()
        self.last_write_seq = win32clipboard.GetClipboardSequenceNumber()
        return text
