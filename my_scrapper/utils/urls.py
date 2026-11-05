__all__ = [
    "get_clean_url",
    "is_same_page_url",
    "get_redirection_clean_url",
]


def get_clean_url(url: str) -> str:
    """URL에서 프래그먼트(#...)만 제거하고, 쿼리스트링(?...)은 온전하게 보존하여 반환합니다."""
    return url.split("#")[0]


def is_same_page_url(url1: str, url2: str) -> bool:
    """두 URL이 실질적으로 같은 페이지를 가리키는지 확인합니다 (쿼리스트링은 유지, 프래그먼트 #만 제거)."""
    return get_clean_url(url1) == get_clean_url(url2)


def get_redirection_clean_url(url: str) -> str:
    """리다이렉션 페이지 검사용: 쿼리스트링(?...) 및 프래그먼트(#...)를 모두 제거하여 베이스 URL을 반환합니다."""
    return url.split("?")[0].split("#")[0]
