from urllib.parse import urlparse

from common.schedule_db import upsert_by_source
from my_scrapper.scrapper import MiguScrapper     # 크롤러 인터페이스
from my_scrapper.storage import ExtractedSchedule # 크롤링된 일정

"""크롤러와 통합db를 연결하는 부분"""

def import_schedules_from_crawler_db_to_common_db(scrapper: MiguScrapper, path:str)->list[tuple[int,str]]:
    """
    크롤러 db에 추출되어 있는 일정을 통합db로 옮기는 코드
        scrapper: 크롤러 db와 연결된 크롤러
        path: 통합db의 경로
    출력:
        옮겨진 일정들의 (공유db id, "inserted"|"updated") 목록
    """
    # url로부터 페이지 제목 매핑해놓음
    url_list = scrapper.get_filtered_urls()
    url_title_dict = {}
    rets:list[tuple[int,str]] = []
    for i in range(len(url_list)):
        cur_url   = url_list[i].url
        cur_title = url_list[i].title
        url_title_dict[cur_url] = cur_title
    # 스케듈을 크롤러로 부터 받아옴
    schedules = scrapper.get_schedules()
    # 각 스케듈에 대해서 add_schedule 호출
    for schedule in schedules:
        # 필드 찾기
        url = schedule.url
        title = url_title_dict.get(url) or url # 필터 목록에 없거나 제목이 없으면 url사용
        content = schedule.summary
        due_at = schedule.end_date
        start_at = schedule.start_date
        source = f"crawler:{urlparse(url).netloc or 'unknown'}" # 팀 규칙: crawler:<사이트>
        source_id = schedule.id
        # 넣기
        rets.append(upsert_by_source(path, source=source, source_id=str(source_id), title=title,
                                     content=content, due_at=due_at, start_at=start_at, url=url))
        # 크롤러 db에서 해당하는 url 비활성화(재수집 방지)
        scrapper.deactivate_url(url)
    # 비활성화된 url관련된 것들 삭제 
    scrapper.remove_deactivated_things()
    return rets