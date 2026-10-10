import unittest,tempfile
from unittest.mock import patch
from web.platform import Platform,projection,checked

class AdapterTests(unittest.TestCase):
    def test_product_prices_and_safe_urls(self):
        item=projection({'wareId':123456,'wareName':'<font>机械</font>键盘','jdPrice':'199.00','shopName':'商店'})
        self.assertEqual(item['price'],'199.00');self.assertEqual(item['title'],'机械键盘')
        self.assertEqual(Platform.resolve('https://item.jd.com/123456.html'),'123456')
        with self.assertRaises(ValueError):Platform.resolve('https://evil.example/123456.html')
    def test_risk_response_cannot_be_empty_success(self):
        with self.assertRaises(ValueError):checked({'disposal':{'risk':True}})
        with tempfile.TemporaryDirectory() as d:
            platform=Platform(d)
            with patch('web.platform.JdAPI.search',return_value={'code':0,'data':{}}):
                with self.assertRaises(ValueError):platform.search('键盘',1,'default')
