from pandas import DataFrame

from .chat_processor import ChatProcessor
from .default.processor import DefaultProcessor

class PandasProcessor(ChatProcessor):
    def __init__(self):
        super().__init__()
        self.processor = DefaultProcessor()
        self.column = ['datetime', 'elapsed', 'authorName', 'message', 'superchat', 'type', 'authorChannel']
        self.row = []
    
    def process(self, chat_components: list):
        if chat_components is None or len(chat_components) == 0:
            return
        self.row.extend(         
            [
                c.datetime,
                c.elapsedTime,
                c.author.name,
                c.message,
                c.amountString,
                c.author.type,
                c.author.channelId
            ]
                for c in self.processor.process(chat_components).items
        )

        return DataFrame(self.row, columns=self.column)
