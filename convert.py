import os
import re
import json
import html
import zipfile
import tempfile
import requests
from datetime import datetime, timedelta

# 1. Знаходження актуального посилання на архів з ЄДР
NAIS_PAGE_URL = "https://nais.gov.ua/m/ediniy-derjavniy-reestr-yuridichnih-osib-fizichnih-osib-pidpriemtsiv-ta-gromadskih-formuvan"

print("Шукаємо актуальне посилання на архів на сайті НАІС...")

session = requests.Session()
session.headers.update({
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
    'Accept-Language': 'uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7',
    'Referer': 'https://nais.gov.ua/'
})

zip_url = None

try:
    res = session.get(NAIS_PAGE_URL, timeout=30)
    res.raise_for_status()

    matches = re.findall(r'href=["\'](https?://[^"\']+\.zip)["\']', res.text, re.IGNORECASE)
    if not matches:
        matches = re.findall(r'href=["\'](/[^"\']+\.zip)["\']', res.text, re.IGNORECASE)
        if matches:
            zip_url = "https://nais.gov.ua" + matches[0]
    else:
        zip_url = matches[0]
except Exception as e:
    print(f"Попередження при парсингу сторінки: {e}")

if not zip_url:
    raise Exception("Не вдалося визначити посилання на ZIP-архів зі сторінки НАІС!")

print(f"Знайдено посилання: {zip_url}")

# 2. Завантаження ZIP-архіву
temp_dir = os.environ.get("RUNNER_TEMP", tempfile.gettempdir())
zip_path = os.path.join(temp_dir, "edr_dump.zip")
extract_dir = os.path.join(temp_dir, "edr_xml")

print(f"Завантаження архіву у {zip_path}...")
with session.get(zip_url, stream=True, timeout=180) as r:
    r.raise_for_status()
    with open(zip_path, 'wb') as f:
        for chunk in r.iter_content(chunk_size=8192*16):
            f.write(chunk)

# Перевірка чи дійсно це ZIP-файл
if not zipfile.is_zipfile(zip_path):
    with open(zip_path, 'r', encoding='utf-8', errors='ignore') as f:
        preview = f.read(500)
    raise Exception(f"Завантажений файл не є ZIP-архівом! Вміст початку файлу:\n{preview}")

print("Архів успішно завантажено. Розпакування ZIP...")

# 3. Пошук та розпакування XML для Юридичних осіб (*_UO_*.xml)
uo_xml_path = None
os.makedirs(extract_dir, exist_ok=True)

with zipfile.ZipFile(zip_path, 'r') as zip_ref:
    for file_info in zip_ref.infolist():
        if file_info.filename.endswith('.xml') and ('_UO_' in file_info.filename or 'UO_FULL' in file_info.filename):
            print(f"Знайдено XML файл юросіб: {file_info.filename}")
            zip_ref.extract(file_info, extract_dir)
            uo_xml_path = os.path.join(extract_dir, file_info.filename)
            break

if not uo_xml_path or not os.path.exists(uo_xml_path):
    raise Exception("XML-файл юридичних осіб (*_UO_*.xml) не знайдено в архіві!")

os.remove(zip_path)
print("ZIP-архів видалено. Починаємо обробку XML...")

# 4. Нормалізація та обробка даних
def fix_mojibake(text):
    if not text: return ''
    s = str(text)
    try:
        if 'Ð' in s or 'Ñ' in s or 'Â' in s:
            s = s.encode('latin1').decode('utf-8')
    except Exception:
        pass
    s = html.unescape(s)
    s = re.sub(r'[\x00-\x1F\x7F-\x9F]', '', s)
    return re.sub(r'\s+', ' ', s).strip()

def clean_val(val):
    if not val: return ''
    val = re.sub(r'22[ТT]|18[ТT]', '«', val)
    val = val.replace('&quot;', '"').replace('&apos;', "'").replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
    val = fix_mojibake(val)
    return re.sub(r'\s+', ' ', val).strip()

def get_field(tags, text):
    for tag in tags:
        m = re.search(rf'<{tag}\b[^>]*>(.*?)</{tag}>', text, re.DOTALL | re.IGNORECASE)
        if m and m.group(1).strip():
            return m.group(1).strip()
    return ''

def process_signer(text):
    if not text: return ''
    text = text.split(';')[0].split(' - керівник')[0].split(' - підписант')[0].strip()
    return text

def get_list_fields(tag_names, text):
    results = []
    for tag in tag_names:
        matches = re.findall(rf'<{tag}\b[^>]*>(.*?)</{tag}>', text, re.DOTALL | re.IGNORECASE)
        for m in matches:
            clean_m = clean_val(m)
            clean_m = re.sub(r'<[^>]+>', ' ', clean_m).strip()
            clean_m = process_signer(clean_m)
            if clean_m and clean_m not in results:
                results.append(clean_m)
    return results

def get_compact_registration(reg_raw):
    if not reg_raw: return ''
    parts = [p.strip() for p in reg_raw.split(';') if p.strip()]
    if len(parts) >= 3:
        return f"{parts[0]}; {parts[-1]}"
    return reg_raw

def parse_date(date_str):
    try:
        m = re.search(r'\b(\d{2})\.(\d{2})\.(\d{4})\b', date_str)
        if m:
            day, month, year = map(int, m.groups())
            return datetime(year, month, day)
    except Exception:
        pass
    return None

detected_encoding = 'cp1251'
with open(uo_xml_path, 'rb') as test_f:
    raw_head = test_f.read(1000)
    if raw_head.startswith(b'\xff\xfe') or raw_head.startswith(b'\xfe\xff'):
        detected_encoding = 'utf-16'
    elif b'encoding="utf-16"' in raw_head.lower() or b'encoding=\'utf-16\'' in raw_head.lower():
        detected_encoding = 'utf-16'
    elif b'encoding="utf-8"' in raw_head.lower() or b'encoding=\'utf-8\'' in raw_head.lower():
        detected_encoding = 'utf-8'

chunks_dir = 'edr_chunks'
os.makedirs(chunks_dir, exist_ok=True)
files_dict = {}

CUTOFF_DATE = datetime.now() - timedelta(days=180)
sub_re = re.compile(r'<(SUBJECT|RECORD|DATA|ROW|UO|OBJECT)\b[^>]*>(.*?)</\1>', re.DOTALL | re.IGNORECASE)

count = 0
skipped_terminated = 0

try:
    with open(uo_xml_path, "r", encoding=detected_encoding, errors="replace") as f:
        buffer = ""
        while True:
            chunk = f.read(1024 * 1024 * 5)
            if not chunk:
                break
            buffer += chunk
            
            matches = list(sub_re.finditer(buffer))
            if not matches:
                continue

            last_end = 0
            for m in matches:
                block = m.group(2)
                last_end = m.end()

                term_info = clean_val(get_field(['TERMINATED_INFO'], block))
                if term_info:
                    term_date = parse_date(term_info)
                    if term_date and term_date < CUTOFF_DATE:
                        skipped_terminated += 1
                        continue

                eid = clean_val(get_field(['EDRPOU', 'CODE', 'USREOU', 'TAX_CODE', 'IPN'], block))
                if not eid:
                    continue

                name = clean_val(get_field(['NAME', 'FULL_NAME', 'NAME_UO'], block))
                short_name = clean_val(get_field(['SHORT_NAME', 'SHORT_NAME_UO'], block))
                status = clean_val(get_field(['STAN', 'STATE', 'STATUS', 'STATE_NAME'], block))
                reg = get_compact_registration(clean_val(get_field(['REGISTRATION', 'REG_DATE', 'REG_NUM'], block)))
                signers = get_list_fields(['SIGNER'], block)

                rec = {
                    "id": eid,
                    "name": name,
                    "short_name": short_name,
                }
                if status and status.strip().lower() != 'зареєстровано':
                    rec["status"] = status
                if reg:
                    rec["registration"] = reg
                if signers:
                    rec["signers"] = signers

                prefix = eid[:3] if len(eid) >= 3 else 'other'
                if prefix not in files_dict:
                    files_dict[prefix] = open(os.path.join(chunks_dir, f"{prefix}.json"), 'w', encoding='utf-8')

                files_dict[prefix].write(json.dumps(rec, ensure_ascii=False, separators=(',', ':')) + '\n')
                count += 1

                if count % 100000 == 0:
                    print(f"Оброблено {count} компаній...")

            buffer = buffer[last_end:]

finally:
    for fp in files_dict.values():
        fp.close()

try:
    os.remove(uo_xml_path)
except Exception:
    pass

print(f"\nУСПІШНО! Згенеровано чанки для {count} юридичних осіб.")
