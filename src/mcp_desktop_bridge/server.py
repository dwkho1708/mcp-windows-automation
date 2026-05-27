import os
import sys
import yaml
from mcp.server.fastmcp import FastMCP
import mcp_desktop_bridge.automation as auto

# 1. 설정 파일 로드 함수
def load_config() -> dict:
    """
    작업 공간 또는 소스 폴더 기준의 config.yaml을 로드합니다.
    """
    config_paths = [
        os.path.join(os.getcwd(), "config.yaml"),
        os.path.join(os.path.dirname(__file__), "..", "..", "config.yaml")
    ]
    for path in config_paths:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return yaml.safe_load(f) or {}
            except Exception as e:
                print(f"Warning: Failed to load config from {path}: {e}", file=sys.stderr)
    return {}

# 2. FastMCP 서버 생성
# stdio 전송 방식을 사용하며, 서버 이름 설정
mcp = FastMCP("mcp-windows-automation")

# 3. MCP 도구 등록
@mcp.tool()
def list_active_windows() -> list[dict]:
    """
    현재 Windows OS에 활성화되어 켜져 있는 visible GUI 창 타이틀 목록을 조회합니다.
    특정 제어 대상 앱의 타이틀(window_title)이나 프로세스명(process_name)을 확인하는 가이드 용도로 사용합니다.
    """
    try:
        return auto.list_windows()
    except Exception as e:
        return [{"error": f"Failed to list windows: {str(e)}"}]

@mcp.tool()
def ask_desktop_app(app_name: str, prompt: str, wait_seconds: int = None) -> str:
    """
    지정한 데스크톱 앱(예: Codex, Notepad, ChatGPT 등)을 포커스하고, 
    질문(prompt)을 타이핑하여 보낸 뒤 결과를 복사하여 답변 텍스트로 반환합니다.
    """
    try:
        config = load_config()
        return auto.send_query_to_window(
            app_name=app_name, 
            prompt=prompt, 
            config=config, 
            wait_seconds=wait_seconds
        )
    except Exception as e:
        return f"Error executing query on '{app_name}': {str(e)}"

@mcp.tool()
def send_keys_to_app(app_name: str, keys: str) -> str:
    """
    지정한 데스크톱 앱(예: Codex, Notepad 등)에 단축키 또는 임의의 키 이벤트를 직접 전송합니다.
    keys 예시: '^s' (Ctrl+S로 저장), '^n' (Ctrl+N으로 새창), '{ENTER}', '{TAB}' 등.
    """
    try:
        config = load_config()
        return auto.send_keys_to_window(
            app_name=app_name,
            keys=keys,
            config=config
        )
    except Exception as e:
        return f"Error sending keys to '{app_name}': {str(e)}"

def main():
    """
    진입점 함수. stdio 방식으로 MCP 서버를 작동시킵니다.
    """
    mcp.run()

if __name__ == "__main__":
    main()
