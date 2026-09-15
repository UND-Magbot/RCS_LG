"""
AutoXing SDK Token Manager.
로그인 후 AutoXing API 호출에 필요한 토큰을 관리합니다.
토큰 캐싱 및 만료 시 자동 재발급을 지원합니다.
"""
import hashlib
import time
import json
import requests

from app.autoxing_api.config import config


class TokenManager:
    """
    AutoXing SDK 토큰 매니저.
    - get_token(): 캐싱된 토큰 반환 (만료 시 자동 갱신)
    - get_token_from_server(): 서버에서 새 토큰 발급
    """

    def __init__(self) -> None:
        self.token = None
        self.expire_time = None
        self.key = None
        self.issued_at = 0
        self.ok = False

    def get_token(self) -> tuple[bool, str | None, str]:
        """캐싱된 토큰이 유효하면 반환, 만료되었으면 재발급."""
        if self.ok:
            current_time = time.time()
            if current_time < self.issued_at / 1000.0 + self.expire_time:
                return True, self.token, "Cached token (valid)"

        return self.get_token_from_server()

    def get_token_from_server(self) -> tuple[bool, str | None, str]:
        """AutoXing 인증 서버에서 새 토큰을 발급받습니다."""
        url = config["URLPrefix"] + "/auth/v1.1/token"

        timestamp = int(time.time() * 1000)
        sign = hashlib.md5(
            (config["APPID"] + str(timestamp) + config["APPSecret"]).encode()
        ).hexdigest()

        data = {
            "appId": config["APPID"],
            "timestamp": timestamp,
            "sign": sign,
        }

        try:
            r = requests.post(
                url,
                headers={"Authorization": config["Authorization"]},
                json=data,
                timeout=5,
            )

            if r.status_code == 200:
                ret_data = json.loads(r.text)

                if ret_data["status"] == 200:
                    self.key = ret_data["data"]["key"]
                    self.token = ret_data["data"]["token"]
                    self.expire_time = ret_data["data"]["expireTime"]
                    self.issued_at = timestamp
                    self.ok = True
                    return True, self.token, "Token issued successfully"

                # AutoXing API가 200 OK이지만 내부 status가 200이 아닌 경우
                self.ok = False
                return False, None, f"AutoXing API error: status={ret_data.get('status')}, msg={ret_data.get('msg', r.text[:200])}"

            # HTTP status가 200이 아닌 경우
            auth_header = config["Authorization"]
            masked = auth_header[:12] + "..." + auth_header[-4:] if len(auth_header) > 16 else auth_header
            self.ok = False
            return False, None, (
                f"HTTP {r.status_code}: {r.text[:200]}"
                f" | URL: {url}"
                f" | APPID: {config['APPID']}"
                f" | Auth header: {masked}"
            )

        except requests.RequestException as e:
            self.ok = False
            return False, None, f"Connection error: {e}"


# 모듈 레벨 싱글턴 인스턴스 (앱 전체에서 공유)
ax_token_manager = TokenManager()
