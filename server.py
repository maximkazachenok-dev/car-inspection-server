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

app = Flask(__name__)
CORS(app)

BOT_TOKEN      = os.environ.get('BOT_TOKEN', '')
CHAT_ID        = os.environ.get('CHAT_ID', '')
SUPABASE_URL   = os.environ.get('SUPABASE_URL', '')
SUPABASE_KEY   = os.environ.get('SUPABASE_KEY', '')
ANTHROPIC_KEY  = os.environ.get('ANTHROPIC_KEY', '')

TELEGRAM_API  = f'https://api.telegram.org/bot{BOT_TOKEN}'
SUPABASE_REST = f'{SUPABASE_URL}/rest/v1'
SUPABASE_STOR = f'{SUPABASE_URL}/storage/v1'

SUPABASE_HEADERS = {
    'apikey': SUPABASE_KEY,
    'Authorization': f'Bearer {SUPABASE_KEY}',
    'Content-Type': 'application/json',
}

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
    content = [{'type': 'text', 'text': (
        'Ты эксперт по осмотру транспортных средств. '
        'Осмотри каждое фото и выяви ТОЛЬКО видимые повреждения: вмятины, царапины, трещины, сколы краски, сломанные элементы. '
        'Формат ответа — строго следующий:\n'
        '🔍 АНАЛИЗ ПОВРЕЖДЕНИЙ\n'
        'Спереди: [повреждения или "✅ без повреждений"]\n'
        'Сзади: [повреждения или "✅ без повреждений"]\n'
        'Левый бок: [повреждения или "✅ без повреждений"]\n'
        'Правый бок: [повреждения или "✅ без повреждений"]\n'
        'Салон: [повреждения или "✅ без повреждений"]\n'
        'Итог: [1-2 предложения]\n\n'
        'Правила:\n'
        '- Каждая зона — одна строка, максимум 10 слов\n'
        '- Пиши только факты, никаких рассуждений\n'
        '- Если зоны нет на фото — пропусти её\n'
        'Отвечай только на русском языке.'
    )}]
    for zone in ZONE_ORDER:
        if zone not in photo_bytes_dict:
            continue
        compressed = compress_image(photo_bytes_dict[zone])
        img_b64 = base64.standard_b64encode(compressed).decode('utf-8')
        content.append({'type': 'text', 'text': f'Зона: {ZONE_LABELS[zone]}'})
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


def send_telegram_message_spoiler(chat_id, text):
    """Отправляет текст в Telegram со spoiler — диспетчер разворачивает нажатием."""
    try:
        import html as _html
        safe = _html.escape(text)
        spoiler_msg = f'<tg-spoiler>{safe}</tg-spoiler>'
        requests.post(
            f'{TELEGRAM_API}/sendMessage',
            json={'chat_id': chat_id, 'text': spoiler_msg, 'parse_mode': 'HTML'},
            timeout=15
        )
    except Exception as e:
        print(f'Telegram spoiler error: {e}')


@app.route('/', methods=['GET'])
def health():
    return 'OK', 200


@app.route('/webhook', methods=['POST'])
def webhook():
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
def submit():
    try:
        fio      = request.form.get('fio', '').strip()
        gosnomer = request.form.get('gosnomer', '').strip().upper()
        type_    = request.form.get('type', '').strip()
        date     = request.form.get('date', '').strip()
        vehicle_type  = request.form.get('vehicle_type', 'Тягач').strip()
        trailer_type  = request.form.get('trailer_type', '').strip()
        moto_hours    = request.form.get('moto_hours', '').strip()
        mileage       = request.form.get('mileage', '').strip()
        check_doc     = request.form.get('check_doc_status', '').strip()
        check_doc_cmt = request.form.get('check_doc_comment', '').strip()

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

        # 1. Читаем фото
        photo_bytes = {}
        for zone in ZONE_ORDER:
            if zone not in request.files:
                continue
            file = request.files[zone]
            if not file:
                continue
            fb = file.read()
            if fb and len(fb) >= 100:
                # Сжимаем на сервере — защита от больших фото с iOS
                if len(fb) > 1500 * 1024:
                    fb = compress_image(fb)
                photo_bytes[zone] = fb

        if not photo_bytes:
            return jsonify({'ok': False, 'error': 'Нет фотографий'}), 400

        # 2. Загружаем фото в Supabase Storage
        photo_urls = {}
        for zone, fb in photo_bytes.items():
            filename = f'{safe_gos}/{inspection_id}/{zone}.jpg'
            url = upload_photo_to_supabase(fb, filename)
            if url:
                photo_urls[zone] = url

        # 3. Сохраняем в БД
        db_record = {
            'id': inspection_id, 'fio': fio, 'gosnomer': gosnomer,
            'type': type_, 'inspection_date': date, 'mileage': mileage,
            'photo_front': photo_urls.get('front'), 'photo_back': photo_urls.get('back'),
            'photo_left': photo_urls.get('left'), 'photo_right': photo_urls.get('right'),
            'photo_salon': photo_urls.get('salon'), 'photo_sleep': photo_urls.get('sleep'),
        }
        db_saved = save_inspection_to_db(db_record)

        # 4. Telegram + ИИ — в фоновом потоке
        def run_background(pb, gos, tp, dt, ml_str, fio_val, db_ok, extra_lines=''):
            print(f'[BG] Starting background for {gos} {dt}')
            try:
                caption = (
                    f"🚛 *ОСМОТР {vehicle_type.upper()}*\n\n"
                    f"👤 {fio_val}\n🔢 {gos}\n📋 {tp.upper()}\n📅 {dt}\n🛣 Пробег: {ml_str}{extra_lines}"
                )
                if db_ok:
                    caption += '\n✅ _Сохранено в базе данных_'

                media = []
                files_dict = {}
                for i, zone in enumerate(ZONE_ORDER):
                    if zone not in pb:
                        continue
                    attach_name = f'photo_{zone}'
                    files_dict[attach_name] = (f'{zone}.jpg', pb[zone], 'image/jpeg')
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
                        import html as _html
                        # Очищаем пустые строки
                        lines = [l for l in ai_report.split('\n') if l.strip()]
                        preview_lines = lines[:4]
                        rest_lines = lines[4:]
                        header = f"\U0001f916 <b>АНАЛИЗ ИИ</b> \u2014 {_html.escape(gos)} \u2022 {_html.escape(tp)} \u2022 {_html.escape(dt)}"
                        preview = '\n'.join(_html.escape(l) for l in preview_lines)
                        if rest_lines:
                            rest = '\n'.join(_html.escape(l) for l in rest_lines)
                            chunk_size = 3500
                            if len(rest) <= chunk_size:
                                full_msg = header + '\n\n' + preview + '\n<tg-spoiler>' + rest + '</tg-spoiler>'
                                r = requests.post(
                                    f'{TELEGRAM_API}/sendMessage',
                                    json={'chat_id': CHAT_ID, 'text': full_msg, 'parse_mode': 'HTML'},
                                    timeout=15
                                )
                                print(f'[BG] AI msg sent: {r.status_code} {r.text[:200]}')
                                # Если HTML отклонён — отправляем без форматирования
                                if r.status_code != 200:
                                    plain = '\n'.join(lines)
                                    r2 = requests.post(
                                        f'{TELEGRAM_API}/sendMessage',
                                        json={'chat_id': CHAT_ID, 'text': '🤖 АНАЛИЗ ИИ\n\n' + plain},
                                        timeout=15
                                    )
                                    print(f'[BG] AI fallback sent: {r2.status_code}')
                            else:
                                first_msg = header + '\n\n' + preview
                                r = requests.post(
                                    f'{TELEGRAM_API}/sendMessage',
                                    json={'chat_id': CHAT_ID, 'text': first_msg, 'parse_mode': 'HTML'},
                                    timeout=15
                                )
                                print(f'[BG] AI first msg: {r.status_code} {r.text[:200]}')
                                for i in range(0, len(rest), chunk_size):
                                    chunk = rest[i:i+chunk_size]
                                    rc = requests.post(
                                        f'{TELEGRAM_API}/sendMessage',
                                        json={'chat_id': CHAT_ID, 'text': '<tg-spoiler>' + chunk + '</tg-spoiler>', 'parse_mode': 'HTML'},
                                        timeout=15
                                    )
                                    print(f'[BG] AI chunk: {rc.status_code}')
                        else:
                            full_msg = header + '\n\n' + preview
                            r = requests.post(
                                f'{TELEGRAM_API}/sendMessage',
                                json={'chat_id': CHAT_ID, 'text': full_msg, 'parse_mode': 'HTML'},
                                timeout=15
                            )
                            print(f'[BG] AI msg sent: {r.status_code} {r.text[:200]}')
                            if r.status_code != 200:
                                plain = '\n'.join(lines)
                                requests.post(
                                    f'{TELEGRAM_API}/sendMessage',
                                    json={'chat_id': CHAT_ID, 'text': '🤖 АНАЛИЗ ИИ\n\n' + plain},
                                    timeout=15
                                )
                    else:
                        print(f'[BG] AI returned empty report for {gos}')
                else:
                    print('[BG] No ANTHROPIC_KEY set')
            except Exception as e:
                print(f'Background error: {e}')
                import traceback; traceback.print_exc()

        extra_lines = trailer_line + doc_line
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
