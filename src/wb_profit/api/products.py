import json
from ..sync import Page, Record
from .transport import ApiError

class Products:
    name = 'products'
    category = 'content'
    def __init__(self, client, initial_cursor=None):
        self.client = client
        self.initial_cursor=initial_cursor
    async def pages(self, cursor):
        position = json.loads(cursor or self.initial_cursor) if (cursor or self.initial_cursor) else {'limit': 100}
        while True:
            response = await self.client.request('POST', '/content/v2/get/cards/list',
                body={'settings': {'sort': {'ascending': True}, 'cursor': position, 'filter': {'withPhoto': -1}}})
            if not isinstance(response, dict) or not isinstance(response.get('cards'), list) or not isinstance(response.get('cursor'), dict):
                raise ApiError('Изменилась схема списка карточек')
            cards, following = response['cards'], response['cursor']
            if not isinstance(following.get('total'), int):
                raise ApiError('Нет количества карточек в курсоре')
            if any(not isinstance(card, dict) or not isinstance(card.get('nmID'), int) for card in cards):
                raise ApiError('В карточке нет nmID')
            if cards and ('updatedAt' not in following or 'nmID' not in following):
                raise ApiError('Нет позиции для последующего обновления карточек')
            completed = following['total'] < 100
            new_position = {**position,'limit': 100, **{k: following[k] for k in ('updatedAt','nmID') if k in following}}
            if not completed and (new_position == position or 'updatedAt' not in new_position or 'nmID' not in new_position):
                raise ApiError('Курсор карточек не продвигается')
            yield Page(tuple(Record(str(card['nmID']), card) for card in cards), json.dumps(new_position, sort_keys=True), completed)
            if completed:
                return
            position = new_position
