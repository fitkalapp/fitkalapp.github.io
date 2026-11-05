"""FİT KAL Instagram otomatik paylaşım (GitHub Actions'ta çalışır; bilgisayar kapalıyken de).

plan.json   -> paylaşılacaklar: {id, when (ISO, +03:00), kind: IMAGE | STORY | REELS, url, caption}
state.json  -> paylaşılanlar (id -> media_id), denemeler, token yenileme zamanı

Ortam değişkenleri (GitHub → Settings → Secrets and variables → Actions):
  IG_TOKEN    (secret)  Instagram kullanıcı erişim anahtarı (instagram_business_basic + instagram_business_content_publish)
  IG_USER_ID  (isteğe bağlı) Instagram hesap kimliği; varsayılan @fitkal_diyetveegzersiz
  GH_PAT      (secret, isteğe bağlı) yalnızca bu depoda "Secrets: read and write" izinli anahtar; varsa IG_TOKEN haftada bir yenilenip
                        secret güncellenir (60 günlük süre hiç dolmaz)
  IG_HOST     (variable, isteğe bağlı) graph.facebook.com (varsayılan, işletme portföyü sistem kullanıcısı) ya da graph.instagram.com
  IG_PAUSED   (variable, isteğe bağlı) "1" ise hiçbir şey paylaşılmaz

python ig_publish.py            -> vakti gelenleri paylaş
python ig_publish.py --dry-run  -> yalnızca ne paylaşılacağını yaz
python ig_publish.py --check    -> anahtarı ve hesabı doğrula
"""
import base64
import datetime as dt
import json
import os
import sys
import time

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLAN = os.path.join(ROOT, 'instagram', 'plan.json')
STATE = os.path.join(ROOT, 'instagram', 'state.json')
HOST = os.environ.get('IG_HOST') or 'graph.facebook.com'   # sistem kullanıcısı anahtarı (süresiz)
VER = os.environ.get('IG_API_VERSION') or 'v23.0'
API = f'https://{HOST}/{VER}'
TOKEN = os.environ.get('IG_TOKEN', '')
UID = os.environ.get('IG_USER_ID') or '17841423519594731'   # @fitkal_diyetveegzersiz
MAX_LATE = dt.timedelta(hours=6)   # bundan geç kalan paylaşım atlanır (eski günün içeriği yeni güne düşmesin)
MAX_TRIES = 3


def now():
    return dt.datetime.now(dt.timezone.utc)


def load(p, default):
    try:
        return json.load(open(p, encoding='utf-8'))
    except FileNotFoundError:
        return default


def save_state(st):
    json.dump(st, open(STATE, 'w', encoding='utf-8'), ensure_ascii=False, indent=1, sort_keys=True)


def api(method, path, **params):
    params['access_token'] = TOKEN
    r = requests.request(method, f'{API}/{path}', data=params if method == 'POST' else None,
                         params=params if method == 'GET' else None, timeout=120)
    j = r.json() if r.content else {}
    if r.status_code >= 400 or 'error' in j:
        raise RuntimeError(f'{method} {path}: {r.status_code} {json.dumps(j.get("error", j), ensure_ascii=False)[:500]}')
    return j


def wait_ready(cid, kind):
    tries = 40 if kind == 'REELS' else 12
    for _ in range(tries):
        s = api('GET', cid, fields='status_code').get('status_code')
        if s in ('FINISHED', 'PUBLISHED'):
            return
        if s in ('ERROR', 'EXPIRED'):
            raise RuntimeError(f'container {cid} durumu {s}')
        time.sleep(15 if kind == 'REELS' else 5)
    raise RuntimeError(f'container {cid} zamanında hazır olmadı')


def publish(it):
    kind = it['kind']
    if kind == 'IMAGE':
        p = dict(image_url=it['url'], caption=it.get('caption', ''))
    elif kind == 'STORY':
        p = dict(media_type='STORIES', image_url=it['url'])
    elif kind == 'REELS':
        p = dict(media_type='REELS', video_url=it['url'], caption=it.get('caption', ''), share_to_feed='true')
    else:
        raise ValueError(kind)
    cid = api('POST', f'{UID}/media', **p)['id']
    wait_ready(cid, kind)
    return api('POST', f'{UID}/media_publish', creation_id=cid)['id']


def refresh_token(st):
    """Haftada bir: IG_TOKEN'ı yenile ve GH_PAT varsa GitHub secret'ını güncelle."""
    pat, repo = os.environ.get('GH_PAT'), os.environ.get('GITHUB_REPOSITORY')
    last = st.get('token_refreshed_at')
    if not pat or not repo or HOST != 'graph.instagram.com':
        return
    if last and now() - dt.datetime.fromisoformat(last) < dt.timedelta(days=7):
        return
    r = requests.get('https://graph.instagram.com/refresh_access_token',
                     params={'grant_type': 'ig_refresh_token', 'access_token': TOKEN}, timeout=60).json()
    new = r.get('access_token')
    if not new:
        print('UYARI: token yenilenemedi:', json.dumps(r)[:300])
        return
    from nacl import encoding, public
    h = {'Authorization': f'Bearer {pat}', 'Accept': 'application/vnd.github+json'}
    k = requests.get(f'https://api.github.com/repos/{repo}/actions/secrets/public-key', headers=h, timeout=60).json()
    box = public.SealedBox(public.PublicKey(k['key'].encode(), encoding.Base64Encoder()))
    enc = base64.b64encode(box.encrypt(new.encode())).decode()
    resp = requests.put(f'https://api.github.com/repos/{repo}/actions/secrets/IG_TOKEN', headers=h,
                        json={'encrypted_value': enc, 'key_id': k['key_id']}, timeout=60)
    if resp.status_code in (201, 204):
        st['token_refreshed_at'] = now().isoformat()
        print('token yenilendi, geçerlilik ~', int(r.get('expires_in', 0)) // 86400, 'gün')
    else:
        print('UYARI: secret güncellenemedi', resp.status_code, resp.text[:200])


def main():
    dry = '--dry-run' in sys.argv
    if '--check' in sys.argv:
        if HOST == 'graph.instagram.com':
            print(api('GET', 'me', fields='user_id,username,account_type'))
            print(api('GET', f'{UID}/content_publishing_limit', fields='quota_usage,config'))
            return
        for path, kw in (('me', dict(fields='id,name')),
                         ('debug_token', dict(input_token=TOKEN)),
                         ('me/accounts', dict(fields='id,name,instagram_business_account{id,username}')),
                         ('me/assigned_pages', dict(fields='id,name')),
                         (UID, dict(fields='id,username')),
                         (f'{UID}/content_publishing_limit', dict(fields='quota_usage,config'))):
            try:
                print(path, '->', json.dumps(api('GET', path, **kw), ensure_ascii=False)[:900])
            except Exception as ex:  # noqa: BLE001
                print(path, 'HATA ->', str(ex)[:400])
        return
    if os.environ.get('IG_PAUSED') == '1':
        print('IG_PAUSED=1 → paylaşım kapalı')
        return
    if not dry and (not TOKEN or not UID):
        print('IG_TOKEN / IG_USER_ID tanımlı değil → bir şey yapılmadı')
        return
    plan = load(PLAN, [])
    st = load(STATE, {'posted': {}, 'tries': {}, 'skipped': {}})
    t = now()
    due = sorted((it for it in plan if it['id'] not in st['posted'] and it['id'] not in st['skipped']
                  and dt.datetime.fromisoformat(it['when']) <= t), key=lambda it: it['when'])
    failed = False
    for it in due:
        late = t - dt.datetime.fromisoformat(it['when'])
        if late > MAX_LATE:
            st['skipped'][it['id']] = f'geç kaldı ({late}), {t.isoformat()}'
            print('atlandı (geç):', it['id'])
            continue
        if dry:
            print('PAYLAŞILACAK:', it['kind'], it['id'], it['url'])
            continue
        try:
            mid = publish(it)
            st['posted'][it['id']] = {'media_id': mid, 'at': now().isoformat(), 'kind': it['kind']}
            st['tries'].pop(it['id'], None)
            print('paylaşıldı:', it['kind'], it['id'], mid)
        except Exception as ex:  # noqa: BLE001
            n = st['tries'].get(it['id'], 0) + 1
            st['tries'][it['id']] = n
            print(f'HATA ({n}/{MAX_TRIES}):', it['id'], ex)
            if n >= MAX_TRIES:
                st['skipped'][it['id']] = f'{MAX_TRIES} deneme başarısız: {str(ex)[:300]}'
            failed = True
        save_state(st)
    if not dry:
        try:
            refresh_token(st)
        except Exception as ex:  # noqa: BLE001
            print('UYARI: token yenileme hatası:', ex)
        save_state(st)
    if failed:
        sys.exit(1)   # Actions'ta kırmızı görünsün, e-posta bildirimi gelsin


if __name__ == '__main__':
    main()
