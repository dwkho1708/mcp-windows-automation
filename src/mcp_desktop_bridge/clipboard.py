import time
import win32clipboard
import win32con

class ClipboardManager:
    """
    클립보드 데이터를 안전하게 백업하고 복원하는 컨텍스트 매니저입니다.
    자동화 동작으로 인해 사용자의 기존 복사 내역이 유실되는 것을 방지합니다.
    """

    def __init__(self):
        self.backup_text = ""

    @staticmethod
    def get_text() -> str:
        """현재 클립보드에 저장된 유니코드 텍스트를 읽어옵니다. 잠금 상태 대응을 위해 재시도 루프를 돕니다."""
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
        return ""

    @staticmethod
    def set_text(text: str):
        """클립보드에 유니코드 텍스트를 설정합니다. 잠금 상태 대응을 위해 재시도 루프를 돕니다."""
        for i in range(5):
            try:
                win32clipboard.OpenClipboard()
                try:
                    win32clipboard.EmptyClipboard()
                    win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
                    return
                finally:
                    win32clipboard.CloseClipboard()
            except Exception:
                time.sleep(0.1)

    def __enter__(self):
        # 진입 시 기존 클립보드 내용을 백업
        self.backup_text = self.get_text()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        # 종료 시 기존 백업본 복원
        self.restore()

    def restore(self):
        """백업해 둔 기존 텍스트를 클립보드에 다시 채워 복원합니다."""
        self.set_text(self.backup_text)
