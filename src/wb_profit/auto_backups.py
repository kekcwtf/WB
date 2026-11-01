"""Автоматические копии: один файл в день, проверка перед ротацией своих версий."""
from dataclasses import dataclass
from datetime import datetime,date,timedelta
from pathlib import Path
import re
import uuid
from .business_dates import MOSCOW
from .backups import BackupError,write_backup

DEFAULTS={'enabled':True,'interval_hours':4,'keep':7}


def validate_options(value):
    if not isinstance(value,dict):raise BackupError('Несовместимые настройки автокопий.')
    value={**DEFAULTS,**value}
    if (type(value['enabled']) is not bool or type(value['interval_hours']) is not int or not 1<=value['interval_hours']<=24 or
            type(value['keep']) is not int or not 3<=value['keep']<=90):
        raise BackupError('Для автокопий допустимы интервал 1–24 часа и 3–90 ежедневных версий.')
    return value


def options(ledger):
    return validate_options(ledger.setting('_app','auto_backup_options',{}))


def has_data(ledger):
    if ledger.setting('_app','stores',[]):return True
    if ledger.db.execute('SELECT 1 FROM operations LIMIT 1').fetchone():return True
    exists=ledger.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_records'").fetchone()
    return bool(exists and ledger.db.execute('SELECT 1 FROM source_records LIMIT 1').fetchone())


def backup_folder(database):
    return Path(database).resolve().parent/'backups'/'automatic'


def target_path(ledger,database,now):
    lineage=ledger.setting('_app','auto_backup_lineage')
    if not isinstance(lineage,str) or not re.fullmatch('[0-9a-f]{32}',lineage):return None
    return backup_folder(database)/f'auto-{lineage}-{now.astimezone(MOSCOW).date().isoformat()}.sqlite'


def backup_due(ledger,database,now=None):
    now=now or datetime.now(MOSCOW)
    if now.tzinfo is None:raise ValueError('Для автокопии нужен часовой пояс')
    config=options(ledger)
    if not config['enabled'] or not has_data(ledger):return False
    state=ledger.setting('_app','auto_backup_state')
    target=target_path(ledger,database,now)
    try:
        if not isinstance(state,dict) or target is None or not target.is_file():return True
        stamp=datetime.fromisoformat(state['success_at'])
        stat=target.stat()
        return not (stamp.tzinfo is not None and timedelta(0)<=now-stamp<timedelta(hours=config['interval_hours']) and
                    state['filename']==target.name and state['size']==stat.st_size and state['mtime_ns']==stat.st_mtime_ns and
                    state['options']==config)
    except (KeyError,TypeError,ValueError,OSError):return True


@dataclass(frozen=True)
class AutoBackupResult:
    path: Path | None
    created: bool
    removed: tuple[Path,...]
    warnings: tuple[str,...]


def automatic_backup(ledger,database,*,now=None,cancel_check=None):
    now=now or datetime.now(MOSCOW)
    config=options(ledger)
    if not backup_due(ledger,database,now):
        return AutoBackupResult(target_path(ledger,database,now),False,(),())
    lineage=ledger.setting('_app','auto_backup_lineage')
    if lineage is None:
        lineage=uuid.uuid4().hex
        ledger.set_setting('_app','auto_backup_lineage',lineage)
    elif not isinstance(lineage,str) or not re.fullmatch('[0-9a-f]{32}',lineage):
        raise BackupError('Неизвестная группа автокопий; существующие файлы не удалены.')
    target=target_path(ledger,database,now)
    target.parent.mkdir(parents=True,exist_ok=True)
    write_backup(ledger.db,target,cancel_check=cancel_check)
    stat=target.stat()
    ledger.set_setting('_app','auto_backup_state',{'success_at':now.isoformat(),'filename':target.name,
                       'size':stat.st_size,'mtime_ns':stat.st_mtime_ns,'options':config})
    # Ротация не начинается, пока новый файл не записан и не проверен write_backup.
    if cancel_check is not None:cancel_check()
    candidates=[];warnings=[];removed=[]
    today=now.astimezone(MOSCOW).date()
    pattern=re.compile(r'auto-'+lineage+r'-(\d{4}-\d{2}-\d{2})\.sqlite')
    for path in target.parent.iterdir():
        match=pattern.fullmatch(path.name)
        if not match or path.is_symlink() or not path.is_file():continue
        try:day=date.fromisoformat(match[1])
        except ValueError:continue
        if day>today:
            warnings.append('Есть копии с будущей датой: они не удалены; проверьте системные часы.')
            continue
        candidates.append((day,path))
    keep={target,*[path for _day,path in sorted(candidates,reverse=True) if path!=target][:config['keep']-1]}
    for _day,path in candidates:
        if path in keep:continue
        if cancel_check is not None:cancel_check()
        if any(Path(str(path)+suffix).exists() for suffix in ('-wal','-shm','-journal')):
            warnings.append('Используемая SQLite-копия пропущена при ротации.');continue
        try:path.unlink();removed.append(path)
        except OSError:warnings.append('Не удалось удалить старую автокопию; новая копия сохранена.')
    return AutoBackupResult(target,True,tuple(removed),tuple(dict.fromkeys(warnings)))
