"""
AutoXing 공통 HTTP 클라이언트.
토큰이 필요한 API 호출 시 AxToken의 토큰 획득 메소드가 자동으로 호출됩니다.
"""
import json
import requests

from app.autoxing_api.config import config
from app.autoxing_api.AxToken import ax_token_manager


class AxClient:
    """
    AutoXing API 공통 클라이언트.
    - get / post / put / delete 호출 시 토큰을 자동 획득하여 X-Token 헤더에 주입
    - 토큰 만료 시 자동 재발급 (TokenManager 내부 처리)
    - 공통 응답 파싱 포함
    """

    def __init__(self, timeout: int = 5) -> None:
        self.base_url = config["URLPrefix"]
        self.timeout = timeout

    def _get_headers(self) -> dict:
        """토큰을 자동 획득하여 헤더를 구성합니다."""
        ok, token, msg = ax_token_manager.get_token()
        if not ok:
            raise ConnectionError(f"AutoXing 토큰 획득 실패: {msg}")
        return {"X-Token": token}

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        """공통 요청 처리 (토큰 자동 주입)"""
        headers = self._get_headers()
        headers.update(kwargs.pop("headers", {}))
        return requests.request(
            method,
            self.base_url + path,
            headers=headers,
            timeout=kwargs.pop("timeout", self.timeout),
            **kwargs,
        )

    def get(self, path: str, **kwargs) -> requests.Response:
        return self._request("GET", path, **kwargs)

    def post(self, path: str, **kwargs) -> requests.Response:
        return self._request("POST", path, **kwargs)

    def put(self, path: str, **kwargs) -> requests.Response:
        return self._request("PUT", path, **kwargs)

    def delete(self, path: str, **kwargs) -> requests.Response:
        return self._request("DELETE", path, **kwargs)

    def parse_response(self, r: requests.Response) -> tuple[bool, dict | list | None, str]:
        """AutoXing API 공통 응답 파싱.
        Returns: (성공여부, data, 메시지)
        """
        if r.status_code != 200:
            return False, None, f"HTTP {r.status_code}: {r.text[:200]}"

        ret_data = json.loads(r.text)
        if ret_data.get("status") == 200:
            return True, ret_data.get("data"), "OK"

        return False, None, f"API error: status={ret_data.get('status')}, msg={ret_data.get('msg', '')}"


# 모듈 레벨 싱글턴
ax_client = AxClient()
