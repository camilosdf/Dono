# backend/app/pagination.py — Sistema Dono
from pydantic import BaseModel


class PageParams:
    def __init__(self, page: int = 1, page_size: int = 50):
        self.page = max(page, 1)
        self.page_size = min(max(page_size, 1), 200)

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size


class Page(BaseModel):
    items: list
    total: int
    page: int
    page_size: int
