"""
AutoXing Robot Management.
AutoXing SDK를 통해 로봇 목록 조회 및 상태 확인을 수행합니다.
토큰은 AxClient를 통해 자동으로 획득/주입됩니다.
"""
import requests

from app.autoxing_api.AxClient import ax_client


class RobotManager:
    """
    AutoXing 로봇 매니저.
    - get_robot_list(): 로봇 목록 조회
    - get_robot_state(robot_id): 특정 로봇 상태 조회
    """

    def get_robot_list(self, page_size: int = 10, page_num: int = 1) -> tuple[bool, list | None, str]:
        """AutoXing 로봇 목록을 조회합니다."""
        try:
            r = ax_client.post("/robot/v1.1/list", json={
                "pageSize": page_size,
                "pageNum": page_num,
            })
            ok, data, msg = ax_client.parse_response(r)
            if ok and data:
                return True, data.get("list", []), msg
            return False, None, msg

        except (requests.RequestException, ConnectionError) as e:
            return False, None, str(e)

    def get_robot_state(self, robot_id: str) -> tuple[bool, dict | None, str]:
        """특정 로봇의 상태를 조회합니다."""
        try:
            r = ax_client.get(f"/robot/v1.1/{robot_id}/state")
            ok, data, msg = ax_client.parse_response(r)
            return ok, data, msg

        except (requests.RequestException, ConnectionError) as e:
            return False, None, str(e)


# 모듈 레벨 싱글턴 인스턴스
ax_robot_manager = RobotManager()
