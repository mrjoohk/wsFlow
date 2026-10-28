"""Local, read-only extension example. Does not connect to an ERP or use credentials."""


class Demo:
    def __init__(self, context):
        self.company = context['settings'].get('company', '예제 회사')

    def handle(self, method, segments, query, body):
        if method != 'GET' or segments not in ([], ['overview']):
            raise ValueError('ERP 모형은 GET overview만 제공합니다')
        return {'demo': True, 'company': self.company,
                'message': '로컬 확장 구조를 확인하는 합성 자료입니다. 실제 ERP 연결은 없습니다.',
                'items': [{'code': 'DEMO-01', 'name': '예제 자원', 'quantity': 3}]}

    def stop(self):
        return True


def create_module(context):
    return Demo(context)
