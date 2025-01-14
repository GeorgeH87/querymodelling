
class PageQuery:

    page: int = 0
    size: int = 10

    @property
    def offset(self):
        return self.page * self.size

    @property
    def offset_end(self):
        return self.offset + self.size
