from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from .config import UserError


def validate(timezone_name, times):
    ZoneInfo(timezone_name)
    if not times or len(times) != len(set(times)):
        raise UserError('Укажите непустой список разных часов публикации.')
    for value in times:
        try:
            datetime.strptime(value, '%H:%M')
        except ValueError:
            raise UserError('Время должно иметь формат HH:MM.') from None
        if len(value) != 5:
            raise UserError('Время должно иметь формат HH:MM.')
    return sorted(times)


def slots(after, timezone_name, times):
    times = validate(timezone_name, times)
    zone = ZoneInfo(timezone_name)
    day = datetime.fromtimestamp(after, zone).date()
    for offset in range(3660):
        current = day + timedelta(days=offset)
        for value in times:
            hour, minute = map(int, value.split(':'))
            local = datetime(current.year, current.month, current.day, hour, minute, tzinfo=zone)
            stamp = local.timestamp()
            # Skip nonexistent local times at DST change; ambiguous time uses first fold.
            if datetime.fromtimestamp(stamp, zone).replace(tzinfo=None) != local.replace(tzinfo=None):
                continue
            if stamp > after:
                yield stamp
    raise UserError('Расписание заполнено более чем на десять лет.')


def label(stamp, timezone_name):
    if stamp is None:
        return 'не назначено'
    return datetime.fromtimestamp(stamp, ZoneInfo(timezone_name)).strftime('%d.%m.%Y %H:%M') + ' ' + timezone_name


def parse_iso(value):
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        raise UserError('Для переноса укажите время с часовым поясом.')
    return moment.astimezone(timezone.utc).timestamp()
