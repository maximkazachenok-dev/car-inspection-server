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
        'Ты эксперт по осмотру грузовых автомобилей с многолетним опытом. '
        'Внимательно осмотри каждое фото и найди ВСЕ видимые повреждения: '
        'разбитые или треснутые фары/стёкла, вмятины, царапины, сколы краски, '
        'сломанный пластик, деформации кузова, повреждения бампера. '
        'Для каждой зоны напиши конкретно что повреждено и где именно (левая/правая сторона, верх/низ). '
        'Если повреждений нет — напиши "без повреждений". '
        'Отвечай только на русском языке. Будь конкретным и точным.'
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
        requests.post(
            f'{TELEGRAM_API}/sendMessage',
            json={'chat_id': chat_id, 'text': text, 'parse_mode': 'Markdown'},
            timeout=15
        )
    except Exception as e:
        print(f'Telegram message error: {e}')


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
        mileage  = request.form.get('mileage', '').strip()

        if not all([fio, gosnomer, type_, date]):
            return jsonify({'ok': False, 'error': 'Не все поля заполнены'}), 400

        inspection_id = str(uuid.uuid4())
        safe_gos = safe_filename(gosnomer)
        mileage_str = f"{int(mileage):,}".replace(',', ' ') + ' км' if mileage else '—'

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
        def run_background(pb, gos, tp, dt, ml_str, fio_val, db_ok):
            try:
                caption = (
                    f"🚛 *ОСМОТР АВТОМОБИЛЯ*\n\n"
                    f"👤 {fio_val}\n🔢 {gos}\n📋 {tp.upper()}\n📅 {dt}\n🛣 Пробег: {ml_str}"
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
                    ai_report = analyze_photos_with_ai(pb)
                    if ai_report:
                        send_telegram_message(CHAT_ID,
                            f"🤖 *АНАЛИЗ ИИ*\n*{gos} • {tp} • {dt}*\n\n{ai_report}")
            except Exception as e:
                print(f'Background error: {e}')
                import traceback; traceback.print_exc()

        threading.Thread(
            target=run_background,
            args=(photo_bytes, gosnomer, type_, date, mileage_str, fio, db_saved),
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
