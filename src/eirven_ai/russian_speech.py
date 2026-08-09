from __future__ import annotations

from datetime import datetime

_ONES = {0:"ноль",1:"один",2:"два",3:"три",4:"четыре",5:"пять",6:"шесть",7:"семь",8:"восемь",9:"девять",10:"десять",11:"одиннадцать",12:"двенадцать",13:"тринадцать",14:"четырнадцать",15:"пятнадцать",16:"шестнадцать",17:"семнадцать",18:"восемнадцать",19:"девятнадцать"}
_TENS = {20:"двадцать",30:"тридцать",40:"сорок",50:"пятьдесят",60:"шестьдесят",70:"семьдесят",80:"восемьдесят",90:"девяносто"}
_HUNDREDS = {100:"сто",200:"двести",300:"триста",400:"четыреста",500:"пятьсот",600:"шестьсот",700:"семьсот",800:"восемьсот",900:"девятьсот"}
_MONTHS = {1:"января",2:"февраля",3:"марта",4:"апреля",5:"мая",6:"июня",7:"июля",8:"августа",9:"сентября",10:"октября",11:"ноября",12:"декабря"}
_ORDINAL_DAY = {1:"первое",2:"второе",3:"третье",4:"четвёртое",5:"пятое",6:"шестое",7:"седьмое",8:"восьмое",9:"девятое",10:"десятое",11:"одиннадцатое",12:"двенадцатое",13:"тринадцатое",14:"четырнадцатое",15:"пятнадцатое",16:"шестнадцатое",17:"семнадцатое",18:"восемнадцатое",19:"девятнадцатое",20:"двадцатое",21:"двадцать первое",22:"двадцать второе",23:"двадцать третье",24:"двадцать четвёртое",25:"двадцать пятое",26:"двадцать шестое",27:"двадцать седьмое",28:"двадцать восьмое",29:"двадцать девятое",30:"тридцатое",31:"тридцать первое"}
_YEAR_ORDINAL_GENITIVE = {
    1:"первого",2:"второго",3:"третьего",4:"четвёртого",5:"пятого",6:"шестого",7:"седьмого",8:"восьмого",9:"девятого",10:"десятого",
    11:"одиннадцатого",12:"двенадцатого",13:"тринадцатого",14:"четырнадцатого",15:"пятнадцатого",16:"шестнадцатого",17:"семнадцатого",18:"восемнадцатого",19:"девятнадцатого",20:"двадцатого",
    21:"двадцать первого",22:"двадцать второго",23:"двадцать третьего",24:"двадцать четвёртого",25:"двадцать пятого",26:"двадцать шестого",27:"двадцать седьмого",28:"двадцать восьмого",29:"двадцать девятого",30:"тридцатого",31:"тридцать первого",
}



def cardinal(n: int) -> str:
    n=int(n)
    if n < 0:
        return "минус " + cardinal(-n)
    if n in _ONES: return _ONES[n]
    if n in _TENS: return _TENS[n]
    if n in _HUNDREDS: return _HUNDREDS[n]
    if n < 100:
        tens=(n//10)*10
        return f"{_TENS[tens]} {_ONES[n%10]}".strip()
    if n < 1000:
        hundreds=(n//100)*100
        rest=n%100
        return f"{_HUNDREDS[hundreds]} {cardinal(rest) if rest else ''}".strip()
    if n < 10000:
        thousands=n//1000
        rest=n%1000
        if thousands == 1: head="одна тысяча"
        elif thousands == 2: head="две тысячи"
        elif thousands in (3,4): head=f"{cardinal(thousands)} тысячи"
        else: head=f"{cardinal(thousands)} тысяч"
        return f"{head} {cardinal(rest) if rest else ''}".strip()
    return str(n)


def decimal_phrase(value: float, digits: int = 2) -> str:
    """TTS-safe Russian decimal, e.g. 81.25 -> 'восемьдесят один рубль двадцать пять копеек' pieces."""
    rounded=round(float(value), max(0,int(digits)))
    whole=int(abs(rounded))
    factor=10**max(0,int(digits))
    frac=int(round((abs(rounded)-whole)*factor))
    prefix="минус " if rounded < 0 else ""
    if not digits or frac == 0:
        return prefix+cardinal(whole)
    return f"{prefix}{cardinal(whole)} целых {cardinal(frac)} сотых"


def _form(n: int, one: str, few: str, many: str) -> str:
    n=abs(int(n))%100
    if 11 <= n <= 14: return many
    last=n%10
    if last==1: return one
    if 2 <= last <= 4: return few
    return many


def time_phrase(dt: datetime | None = None) -> str:
    dt=dt or datetime.now()
    h,m=dt.hour,dt.minute
    return f"Сейчас {cardinal(h)} {_form(h,'час','часа','часов')} {cardinal(m)} {_form(m,'минута','минуты','минут')}."


def date_phrase(dt: datetime | None = None) -> str:
    dt=dt or datetime.now()
    if 2001 <= dt.year <= 2031:
        year = "две тысячи " + _YEAR_ORDINAL_GENITIVE[dt.year - 2000]
    elif dt.year == 2000:
        year = "двухтысячного"
    else:
        year = cardinal(dt.year)
    return f"Сегодня {_ORDINAL_DAY.get(dt.day, str(dt.day))} {_MONTHS.get(dt.month, str(dt.month))} {year} года."


def russian_weather_condition(text: str) -> str:
    raw=(text or '').strip()
    low=raw.casefold()
    mapping=(
        (("partly cloudy","partly cloudy"),"переменная облачность"),
        (("cloudy","overcast"),"облачно"),
        (("clear","sunny"),"ясно"),
        (("light rain","drizzle"),"небольшой дождь"),
        (("rain","rainy"),"дождь"),
        (("snow","snowy"),"снег"),
        (("fog","mist"),"туман"),
        (("thunder","storm"),"гроза"),
    )
    for keys, value in mapping:
        if any(k in low for k in keys): return value
    return raw
