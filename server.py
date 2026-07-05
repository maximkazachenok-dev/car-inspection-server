import os
import json
import uuid
import base64
import requests
import threading
from io import BytesIO
from PIL import Image
from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_limiter import Limiter          # P1.5: rate limiting
from flask_limiter.util import get_remote_address

app = Flask(__name__)
CORS(app, origins=['https://maximkazachenok-dev.github.io'])  # P1.4: только фронтенд

# P1.5: rate limiting — защита от злоупотреблений платным API
# Gunicorn с 2 воркерами → фактически лимит x2 на воркер, допустимо для внутреннего приложения
limiter = Limiter(app=app, key_func=get_remote_address, default_limits=["500 per day"])

BOT_TOKEN      = os.environ.get('BOT_TOKEN', '')
CHAT_ID        = os.environ.get('CHAT_ID', '')
SUPABASE_URL   = os.environ.get('SUPABASE_URL', '')
SUPABASE_KEY   = os.environ.get('SUPABASE_KEY', '')
ANTHROPIC_KEY  = os.environ.get('ANTHROPIC_KEY', '')

# Безопасность (P0.2, P0.3)
API_TOKEN               = os.environ.get('API_TOKEN', '')
TELEGRAM_WEBHOOK_SECRET = os.environ.get('TELEGRAM_WEBHOOK_SECRET', '')

TELEGRAM_API  = f'https://api.telegram.org/bot{BOT_TOKEN}'
SUPABASE_REST = f'{SUPABASE_URL}/rest/v1'
SUPABASE_STOR = f'{SUPABASE_URL}/storage/v1'

SUPABASE_HEADERS = {
    'apikey': SUPABASE_KEY,
    'Authorization': f'Bearer {SUPABASE_KEY}',
    'Content-Type': 'application/json',
}

def escape_markdown(text):
    """P2.8: экранирует спецсимволы Markdown в пользовательском вводе."""
    for ch in ['_', '*', '`', '[']:
        text = text.replace(ch, '\\' + ch)
    return text


def is_valid_image(fb):
    """P2.7: проверяет что байты являются корректным изображением."""
    try:
        from io import BytesIO
        img = Image.open(BytesIO(fb))
        img.verify()
        return True
    except Exception:
        return False


ZONE_ORDER = ['front', 'back', 'left', 'right', 'salon', 'sleep']
ZONE_LABELS = {
    'front': 'Спереди', 'back': 'Сзади', 'left': 'Левый бок',
    'right': 'Правый бок', 'salon': 'Салон', 'sleep': 'Спальник',
}


def transliterate(text):
    mapping = {
        'А':'A','Б':'B','В':'V','Г':'G','Д':'D','Е':'E','Ё':'YO','Ж':'ZH',
        'З':'Z','И':'I','Й':'Y','К':'K','Л':'L','М':'M','Н':'N','О':'O',
        'П':'P','Р':'R','С':'S','Т':'T','У':'U','Ф':'F','Х':'KH','Ц':'TS',
        'Ч':'CH','Ш':'SH','Щ':'SCH','Ъ':'','Ы':'Y','Ь':'','Э':'E','Ю':'YU','Я':'YA',
    }
    result = ''
    for ch in text.upper():
        result += mapping.get(ch, ch)
    return result


def safe_filename(gos):
    s = transliterate(gos)
    s = s.replace(' ', '_').replace('/', '-').replace('\\', '-')
    return ''.join(c for c in s if c.isalnum() or c in '-_')


def compress_image(img_bytes, max_size_kb=3000, max_dimension=1920):
    try:
        img = Image.open(BytesIO(img_bytes))
        if img.mode not in ('RGB', 'L'):
            img = img.convert('RGB')
        w, h = img.size
        if w > max_dimension or h > max_dimension:
            ratio = min(max_dimension/w, max_dimension/h)
            img = img.resize((int(w*ratio), int(h*ratio)), Image.LANCZOS)
        quality = 85
        while quality >= 40:
            buf = BytesIO()
            img.save(buf, format='JPEG', quality=quality, optimize=True)
            if buf.tell() <= max_size_kb * 1024:
                return buf.getvalue()
            quality -= 10
        return buf.getvalue()
    except Exception as e:
        print(f'Compress error: {e}')
        return img_bytes


def upload_photo_to_supabase(file_bytes, filename):
    url = f'{SUPABASE_STOR}/object/inspection-photos/{filename}'
    headers = {
        'apikey': SUPABASE_KEY,
        'Authorization': f'Bearer {SUPABASE_KEY}',
        'Content-Type': 'image/jpeg',
    }
    resp = requests.post(url, data=file_bytes, headers=headers, timeout=30)
    if resp.status_code in (200, 201):
        return f'{SUPABASE_STOR}/object/public/inspection-photos/{filename}'
    print(f'Supabase upload error: {resp.status_code} {resp.text}')
    return None


def save_inspection_to_db(record):
    url = f'{SUPABASE_REST}/inspections'
    headers = {**SUPABASE_HEADERS, 'Prefer': 'return=representation'}
    resp = requests.post(url, json=record, headers=headers, timeout=15)
    if resp.status_code in (200, 201):
        return True
    print(f'Supabase DB error: {resp.status_code} {resp.text}')
    return False


def get_inspection_from_db(gosnomer, date):
    url = f'{SUPABASE_REST}/inspections'
    params = {
        'gosnomer': f'eq.{gosnomer.upper()}',
        'inspection_date': f'eq.{date}',
        'order': 'created_at.desc',
        'limit': '1',
    }
    resp = requests.get(url, params=params, headers=SUPABASE_HEADERS, timeout=15)
    if resp.status_code == 200:
        data = resp.json()
        return data[0] if data else None
    return None


def analyze_photos_with_ai(photo_bytes_dict):
    if not ANTHROPIC_KEY:
        return None
    zone_labels = list(photo_bytes_dict.keys())
    zones_format = '\n'.join(f'{lbl}: [повреждения или "✅ без повреждений"]' for lbl in zone_labels)
    prompt_text = (
        'Ты эксперт по осмотру транспортных средств. '
        'Осмотри каждое фото и выяви ТОЛЬКО видимые повреждения: вмятины, царапины, трещины, сколы краски, сломанные элементы. '
        'Формат ответа — строго следующий:\n'
        '🔍 АНАЛИЗ ПОВРЕЖДЕНИЙ\n'
        + zones_format + '\n'
        'Итог: [1-2 предложения]\n\n'
        'Правила:\n'
        '- Каждая зона — одна строка, максимум 10 слов\n'
        '- Пиши только факты, никаких рассуждений\n'
        '- Если зоны нет на фото — пропусти её\n'
        'Отвечай только на русском языке.'
    )
    content = [{'type': 'text', 'text': prompt_text}]
    for label, fb in photo_bytes_dict.items():
        compressed = compress_image(fb)
        img_b64 = base64.standard_b64encode(compressed).decode('utf-8')
        content.append({'type': 'text', 'text': f'Зона: {label}'})
        content.append({'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/jpeg', 'data': img_b64}})
    try:
        resp = requests.post(
            'https://api.anthropic.com/v1/messages',
            headers={'x-api-key': ANTHROPIC_KEY, 'anthropic-version': '2023-06-01', 'content-type': 'application/json'},
            json={'model': 'claude-sonnet-4-6', 'max_tokens': 1024, 'messages': [{'role': 'user', 'content': content}]},
            timeout=90
        )
        if resp.status_code == 200:
            return resp.json()['content'][0]['text']
        print(f'Claude API error: {resp.status_code} {resp.text}')
        return None
    except Exception as e:
        print(f'Claude API exception: {e}')
        return None


def compare_inspections_with_ai(insp1, insp2):
    if not ANTHROPIC_KEY:
        return 'ANTHROPIC_KEY не настроен.'
    content = [{'type': 'text', 'text': (
        f'Сравни два осмотра {insp1["gosnomer"]}.\n'
        f'Осмотр 1 (старый): {insp1["inspection_date"]}\n'
        f'Осмотр 2 (новый): {insp2["inspection_date"]}\n\n'
        'Для каждой зоны определи: появились ли новые повреждения или изменений нет. '
        'В конце дай общий вывод. Отвечай на русском языке.'
    )}]
    for zone in ZONE_ORDER:
        url1 = insp1.get(f'photo_{zone}')
        url2 = insp2.get(f'photo_{zone}')
        if not url1 or not url2:
            continue
        content.append({'type': 'text', 'text': f'\n--- {ZONE_LABELS[zone]} ---'})
        for label, url in [(f'Осмотр {insp1["inspection_date"]}', url1), (f'Осмотр {insp2["inspection_date"]}', url2)]:
            try:
                img_resp = requests.get(url, timeout=20)
                if img_resp.status_code == 200:
                    img_b64 = base64.standard_b64encode(img_resp.content).decode('utf-8')
                    content.append({'type': 'text', 'text': label})
                    content.append({'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/jpeg', 'data': img_b64}})
            except Exception as e:
                print(f'Error fetching {url}: {e}')
    try:
        resp = requests.post(
            'https://api.anthropic.com/v1/messages',
            headers={'x-api-key': ANTHROPIC_KEY, 'anthropic-version': '2023-06-01', 'content-type': 'application/json'},
            json={'model': 'claude-haiku-4-5-20251001', 'max_tokens': 2048, 'messages': [{'role': 'user', 'content': content}]},
            timeout=120
        )
        if resp.status_code == 200:
            return resp.json()['content'][0]['text']
        return 'Ошибка при обращении к ИИ.'
    except Exception as e:
        return f'Ошибка: {e}'


def send_telegram_message(chat_id, text):
    try:
        max_len = 4000
        if len(text) <= max_len:
            requests.post(
                f'{TELEGRAM_API}/sendMessage',
                json={'chat_id': chat_id, 'text': text, 'parse_mode': 'Markdown'},
                timeout=15
            )
        else:
            lines = text.split('\n')
            chunk = ''
            for line in lines:
                if len(chunk) + len(line) + 1 > max_len:
                    requests.post(
                        f'{TELEGRAM_API}/sendMessage',
                        json={'chat_id': chat_id, 'text': chunk, 'parse_mode': 'Markdown'},
                        timeout=15
                    )
                    chunk = line
                else:
                    chunk = chunk + '\n' + line if chunk else line
            if chunk:
                requests.post(
                    f'{TELEGRAM_API}/sendMessage',
                    json={'chat_id': chat_id, 'text': chunk, 'parse_mode': 'Markdown'},
                    timeout=15
                )
    except Exception as e:
        print(f'Telegram message error: {e}')


@app.route('/', methods=['GET'])
def health():
    return 'OK', 200


@app.route('/webhook', methods=['POST'])
@limiter.limit('60 per hour')  # P1.5
def webhook():
    # P0.3: проверка подписи Telegram
    secret = request.headers.get('X-Telegram-Bot-Api-Secret-Token', '')
    if TELEGRAM_WEBHOOK_SECRET and secret != TELEGRAM_WEBHOOK_SECRET:
        return jsonify({'ok': False}), 401
    try:
        data = request.json
        message = data.get('message', {})
        text = message.get('text', '').strip()
        chat_id = message.get('chat', {}).get('id')
        if not text or not chat_id:
            return jsonify({'ok': True})
        if text.lower().startswith('сравни'):
            parts = text.split()
            if len(parts) >= 4 and 'и' in parts:
                gosnomer = parts[1]
                idx = parts.index('и')
                date1 = parts[2] if idx > 2 else None
                date2 = parts[idx + 1] if idx + 1 < len(parts) else None
                if not date1 or not date2:
                    send_telegram_message(chat_id, '⚠️ Формат: `сравни АХ5463-5 01.05.2026 и 01.04.2026`')
                    return jsonify({'ok': True})
                send_telegram_message(chat_id, f'🔍 Ищу осмотры {gosnomer}...')
                insp1 = get_inspection_from_db(gosnomer, date1)
                insp2 = get_inspection_from_db(gosnomer, date2)
                if not insp1:
                    send_telegram_message(chat_id, f'❌ Осмотр {gosnomer} от {date1} не найден.')
                    return jsonify({'ok': True})
                if not insp2:
                    send_telegram_message(chat_id, f'❌ Осмотр {gosnomer} от {date2} не найден.')
                    return jsonify({'ok': True})
                send_telegram_message(chat_id, '🤖 Анализирую фото, подождите...')
                result = compare_inspections_with_ai(insp2, insp1)
                send_telegram_message(chat_id, f'🔄 *СРАВНЕНИЕ {gosnomer}*\n📅 {date2} → {date1}\n\n{result}')
            else:
                send_telegram_message(chat_id, '⚠️ Формат: `сравни АХ5463-5 01.05.2026 и 01.04.2026`')
        return jsonify({'ok': True})
    except Exception as e:
        print(f'Webhook error: {e}')
        return jsonify({'ok': True})


@app.route('/submit', methods=['POST'])
@limiter.limit('30 per hour')  # P1.5
def submit():
    # P0.2: проверка токена клиента
    client_token = request.headers.get('X-App-Token', '')
    if not API_TOKEN or client_token != API_TOKEN:
        return jsonify({'ok': False, 'error': 'Unauthorized'}), 401
    try:
        fio      = escape_markdown(request.form.get('fio', '').strip())
        gosnomer = escape_markdown(request.form.get('gosnomer', '').strip()).upper()
        type_    = request.form.get('type', '').strip()
        date     = request.form.get('date', '').strip()
        vehicle_type  = request.form.get('vehicle_type', 'Тягач').strip()

        # ТМЦ — динамический список
        tmc_icons = {'ok': '✅', 'warn': '⚠️', 'bad': '🔴'}
        tmc_items_raw = request.form.getlist('tmc_item')
        tmc_line = ''
        for item_str in tmc_items_raw:
            if ':' in item_str:
                label, status = item_str.rsplit(':', 1)
                icon = tmc_icons.get(status.strip(), '')
                tmc_line += f'\n{icon} {label.strip()}'

        # Доп. поля основной информации
        extra_fields_raw = request.form.getlist('extra_field')
        extra_fields_line = ''
        for item_str in extra_fields_raw:
            if ':' in item_str:
                label, value = item_str.split(':', 1)
                extra_fields_line += f'\n{label.strip()}: {value.strip()}'

        # Голосовые замечания
        voice_notes = request.form.get('voice_notes', '').strip()
        voice_line = f'\n\n\U0001f5e3 *Замечания:* {voice_notes}' if voice_notes else ''
        trailer_type  = request.form.get('trailer_type', '').strip()
        moto_hours    = request.form.get('moto_hours', '').strip()
        mileage       = request.form.get('mileage', '').strip()
        check_doc     = request.form.get('check_doc_status', '').strip()
        check_doc_cmt = escape_markdown(request.form.get('check_doc_comment', '').strip())  # P2.8

        if not all([fio, gosnomer, type_, date]):
            return jsonify({'ok': False, 'error': 'Не все поля заполнены'}), 400

        inspection_id = str(uuid.uuid4())
        safe_gos = safe_filename(gosnomer)
        if mileage:
            mileage_val = f"{int(mileage):,}".replace(',', ' ')
            mileage_str = f"{mileage_val} км"
        else:
            mileage_str = None

        # Доп. строки для прицепа
        trailer_line = ''
        if vehicle_type == 'Полуприцеп':
            if trailer_type:
                trailer_line += f'\n🚚 Тип прицепа: {trailer_type.upper()}'
            if moto_hours:
                trailer_line += f'\n⏱ Мото-часы ДВС: {moto_hours} м/ч'

        check_icons = {'ok': '✅', 'warn': '⚠️', 'bad': '🔴'}
        check_labels = {'ok': 'всё хорошо', 'warn': 'некритичные замечания', 'bad': 'критичные замечания'}
        doc_line = ''
        if check_doc:
            icon = check_icons.get(check_doc, '')
            label = check_labels.get(check_doc, '')
            doc_line = f'\n📋 Документы: {icon} {label}'
            if check_doc_cmt:
                doc_line += f' — {check_doc_cmt}'

        # 1. Читаем фото — новый динамический формат (photo_0, photo_1, ...) с метками
        photo_bytes = {}   # ключ: метка зоны (label), значение: байты
        photo_count = int(request.form.get('photo_count', 0) or 0)
        if photo_count > 0:
            for i in range(photo_count):
                key = f'photo_{i}'
                if key not in request.files:
                    continue
                file = request.files[key]
                if not file:
                    continue
                fb = file.read()
                if fb and len(fb) >= 100 and is_valid_image(fb):  # P2.7
                    if len(fb) > 1500 * 1024:
                        fb = compress_image(fb)
                    label = request.form.get(f'photo_label_{i}', f'Фото {i+1}').strip()
                    photo_bytes[label] = fb
        else:
            # Обратная совместимость со старым форматом (front/back/...)
            for zone in ZONE_ORDER:
                if zone not in request.files:
                    continue
                file = request.files[zone]
                if not file:
                    continue
                fb = file.read()
                if fb and len(fb) >= 100 and is_valid_image(fb):  # P2.7
                    if len(fb) > 1500 * 1024:
                        fb = compress_image(fb)
                    photo_bytes[ZONE_LABELS.get(zone, zone)] = fb

        if not photo_bytes:
            return jsonify({'ok': False, 'error': 'Нет фотографий'}), 400

        # 2. Загружаем фото в Supabase Storage
        photo_urls = {}   # метка → url
        for label, fb in photo_bytes.items():
            safe_label = safe_filename(label)
            filename = f'{safe_gos}/{inspection_id}/{safe_label}.jpg'
            url = upload_photo_to_supabase(fb, filename)
            if url:
                photo_urls[label] = url

        # 3. Сохраняем в БД — первые 6 фото в существующие колонки
        url_list = list(photo_urls.values())
        db_record = {
            'id': inspection_id, 'fio': fio, 'gosnomer': gosnomer,
            'type': type_, 'inspection_date': date, 'mileage': mileage,
            'photo_front': url_list[0] if len(url_list) > 0 else None,
            'photo_back':  url_list[1] if len(url_list) > 1 else None,
            'photo_left':  url_list[2] if len(url_list) > 2 else None,
            'photo_right': url_list[3] if len(url_list) > 3 else None,
            'photo_salon': url_list[4] if len(url_list) > 4 else None,
            'photo_sleep': url_list[5] if len(url_list) > 5 else None,
        }
        db_saved = save_inspection_to_db(db_record)

        # 4. Telegram + ИИ — в фоновом потоке
        def run_background(pb, gos, tp, dt, ml_str, fio_val, db_ok, extra_lines=''):
            print(f'[BG] Starting background for {gos} {dt}')
            try:
                caption = (
                    f"🚛 *ОСМОТР ТРАНСПОРТНОГО СРЕДСТВА*\n\n"
                    f"👤 {fio_val}\n🔢 {gos}\n📋 {tp.upper()}\n📅 {dt}\n🛣 Пробег: {ml_str}{extra_lines}"
                )
                if db_ok:
                    caption += '\n✅ _Сохранено в базе данных_'

                media = []
                files_dict = {}
                for i, (label, fb) in enumerate(pb.items()):
                    attach_name = f'photo_{i}'
                    files_dict[attach_name] = (f'{attach_name}.jpg', fb, 'image/jpeg')
                    media_item = {'type': 'photo', 'media': f'attach://{attach_name}'}
                    if i == 0:
                        media_item['caption'] = caption
                        media_item['parse_mode'] = 'Markdown'
                    media.append(media_item)

                if media:
                    tg = requests.post(
                        f'{TELEGRAM_API}/sendMediaGroup',
                        data={'chat_id': CHAT_ID, 'media': json.dumps(media)},
                        files=files_dict, timeout=90
                    )
                    print(f'Telegram: {tg.status_code}')

                if ANTHROPIC_KEY:
                    print(f'[BG] Starting AI analysis for {gos}')
                    ai_report = analyze_photos_with_ai(pb)
                    if ai_report:
                        print(f'[BG] AI analysis done for {gos}, length={len(ai_report)}')
                        # Очищаем пустые строки
                        lines = [l for l in ai_report.split('\n') if l.strip()]
                        clean_report = '\n'.join(lines)
                        header = f"\U0001f916 *АНАЛИЗ AI-ПОМОЩНИКА* \u2014 {gos} \u2022 {tp} \u2022 {dt}"
                        full_msg = header + '\n\n' + clean_report
                        # Разбиваем на части если длиннее лимита Telegram
                        max_len = 4000
                        if len(full_msg) <= max_len:
                            r = requests.post(
                                f'{TELEGRAM_API}/sendMessage',
                                json={'chat_id': CHAT_ID, 'text': full_msg, 'parse_mode': 'Markdown'},
                                timeout=15
                            )
                            print(f'[BG] AI msg sent: {r.status_code} {r.text[:200]}')
                            if r.status_code != 200:
                                requests.post(
                                    f'{TELEGRAM_API}/sendMessage',
                                    json={'chat_id': CHAT_ID, 'text': full_msg},
                                    timeout=15
                                )
                        else:
                            # Заголовок + тело частями
                            requests.post(
                                f'{TELEGRAM_API}/sendMessage',
                                json={'chat_id': CHAT_ID, 'text': header, 'parse_mode': 'Markdown'},
                                timeout=15
                            )
                            for i in range(0, len(clean_report), 3500):
                                chunk = clean_report[i:i+3500]
                                rc = requests.post(
                                    f'{TELEGRAM_API}/sendMessage',
                                    json={'chat_id': CHAT_ID, 'text': chunk},
                                    timeout=15
                                )
                                print(f'[BG] AI chunk: {rc.status_code}')
                    else:
                        print(f'[BG] AI returned empty report for {gos}')
                else:
                    print('[BG] No ANTHROPIC_KEY set')
            except Exception as e:
                print(f'Background error: {e}')
                import traceback; traceback.print_exc()

        extra_lines = extra_fields_line + trailer_line + doc_line + (('\n\n📦 *ТМЦ:*' + tmc_line) if tmc_line else '') + voice_line
        threading.Thread(
            target=run_background,
            args=(photo_bytes, gosnomer, type_, date, mileage_str, fio, db_saved, extra_lines),
            daemon=True
        ).start()

        # Сразу отвечаем водителю — не ждём отправки
        return jsonify({'ok': True, 'db_saved': db_saved})

    except Exception as e:
        print(f'Server error: {e}')
        import traceback; traceback.print_exc()
        return jsonify({'ok': False, 'error': str(e)}), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
