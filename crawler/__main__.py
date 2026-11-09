"""
python -m crawler [그냥옵션] {행위에 필요한거...} <행위>
<행위>: 
    run -> 크롤러를 실행함
    add -> 시작점 url을 추가함
    remove -> url을 제거함(url이 시작점이 아닌경우에는 비활성화), url과 연관된 것들도 제거함
    set -> 설정을 추가함(설정파일에 추가 후)
    get -> 원하는 내용을 가져옴
    export -> 추출된 일정을 공유db로 보냄!!!!
{행위에 필요한거}:
    --url|-u <url>
    --description <url에 대한 설명>
    --key|-k <제미니 api키>
    --stage|-s <crawl|fetch_contents|filter_urls|extract_schedules|all> : 수행할 단계
    --target|-t <source_urls|urls|filtered_urls|schedules> : 가져올 대상
[그냥옵션]
    --debug : 디버그 모드(기본값: False, 활성화 시 크롤링창이 보임)
"""

import argparse
from dotenv import get_key, set_key
from pathlib import Path
from lms.config import load_settings

from my_scrapper import MiguScrapper
from .crawler_db_connect import import_schedules_from_crawler_db_to_common_db

def main():
    parser = argparse.ArgumentParser(
        prog="python -m my_scrapper", description="내 패키지의 커스텀 CLI 도구입니다."
    )

    # 행위
    parser.add_argument("job", help="작업명: run|add|remove|set|get|export")
    # url
    parser.add_argument(
        "-u",
        "--url",
        type=str,
        help="url 주소들",
    )
    # key
    parser.add_argument(
        "-k",
        "--key",
        type=str,
        help="제미니 api키",
    )
    # stage
    parser.add_argument(
        "-s",
        "--stage",
        type=str,
        default="all",
        help="실행할 단계: crawl|fetch_contents|filter_urls|extract_schedules|all",
    )
    # target
    parser.add_argument(
        "-t",
        "--target",
        type=str,
        help="가져올 대상: source_urls|urls|filtered_urls|schedules",
    )
    # debug
    parser.add_argument(
        "--debug", action="store_true", help="크롤링창을 띄움"
    )

    # env
    env_path = Path(".env")
    if not env_path.exists():
        env_path.touch()

    args = parser.parse_args()
    job, stage, target, url, key, target = args.job, args.stage, args.target, args.url, args.key, args.target
    if job == "set":
        if not key: 
            print("키를 제공해 주세요")
            return
        set_key(env_path, "API_KEY", key)
        print("키 설정완료")
        return

    scrapper = MiguScrapper(api_key=get_key(env_path, "API_KEY"))
    if job == "run":
        match stage:
            case "crawl": scrapper.crawl()
            case "fetch_contents" : scrapper.fetch_contents()
            case "filter_urls" : scrapper.filter_urls()
            case "extract_schedules" : scrapper.extract_schedules()
            case "all" : scrapper.run()
        return
    if job == "add" or job == "remove":
        if url is None: 
            print("url을 제공해 주세요") 
            return
        match job:
            case "add": 
                scrapper.add_source(url)
                print("url이 시작점에 추가됨")
            case "remove" : 
                scrapper.remove_source(url)
                scrapper.deactivate_url(url)
                scrapper.remove_deactivated_things()
                print("제거 또는 비활성화 됨") # 위에 함수들이 실제 제거, 비활성화 한거를 반환하게...
        return
    if job == "get":
        if target is None:
            print("대상을 지정해 주세요")
            return
        match target:
            case "source_urls" : 
                print("크롤링시작점:\n"+"\n".join([f"{obj.url}:{obj.title}" for obj in scrapper.get_sources()]))
            case "urls":
                print("수집된 url들:\n"+"\n".join([f"{obj.url}:{obj.title}" for obj in scrapper.get_crawled_urls()]))
            case "filtered_urls":
                print("필터를 통과한 url들:\n"+"\n".join([f"{obj.url}:{obj.title}" for obj in scrapper.get_filtered_urls()]))
            case "schedules":
                print("추출된 일정들:\n"+"\n".join([f"{obj.url}:{obj.summary}:{obj.start_date or ""}~{obj.end_date or ""}" for obj in scrapper.get_schedules()]))
        return
    if job == "export":
        import_schedules_from_crawler_db_to_common_db(scrapper, str(load_settings().schedule_db))
        print("공유db로 이동완료")

if __name__ == "__main__":
    main()