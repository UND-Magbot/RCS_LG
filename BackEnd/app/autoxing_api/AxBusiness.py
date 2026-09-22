"""
AutoXing Business Management.
비즈니스(사업장) 목록 조회를 수행합니다.
토큰은 AxClient를 통해 자동으로 획득/주입됩니다.
"""
import requests

from app.autoxing_api.AxClient import ax_client


class BusinessManager:
    """
    AutoXing 비즈니스 매니저.
    - get_business_list(): 비즈니스 목록 조회
    - get_business_name_map(): {businessId: name} 딕셔너리 반환
    """

    def get_business_list(self) -> tuple[bool, list | None, str]:
        """비즈니스(사업장) 목록을 조회합니다."""
        try:
            r = ax_client.post("/business/v1.1/list")
            ok, data, msg = ax_client.parse_response(r)
            if ok and data:
                return True, data.get("lists", []), msg
            return False, None, msg

        except (requests.RequestException, ConnectionError) as e:
            return False, None, str(e)

    def get_business_name_map(self) -> dict[str, str]:
        """businessId → name 매핑 딕셔너리를 반환합니다."""
        ok, businesses, _ = self.get_business_list()
        if not ok or not businesses:
            return {}
        return {
            b.get("id", ""): b.get("name", "")
            for b in businesses
        }


# 모듈 레벨 싱글턴 인스턴스
ax_business_manager = BusinessManager()
