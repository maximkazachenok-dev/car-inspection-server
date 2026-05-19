import os
import json
import uuid
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ======= НАСТРОЙКИ =======
BOT_TOKEN     = os.environ.get('BOT_TOKEN', '')
CHAT_ID       = os.environ.get('CHAT_ID', '')
SUPABASE_URL  = os.environ.get('SUPABASE_URL', '')
SUPABASE_KEY  = os.environ.get('SUPABASE_KEY', '')
# =========================

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
    'front': 'Спереди',
    'back':  'Сзади',
    'left':  'Левый бок',
    'right': 'Правый бок',
    'salon': 'Салон',
    'sleep': 'Спальник',
}

@app.route('/', methods=['GET'])
def health():
    return 'OK', 200


def upload_photo_to_supabase(file_bytes, filename):
    """Загружает фото в Supabase Storage, возвращает публичный URL."""
    url = f'{SUPABASE_STOR}/object/inspection-photos/{filename}'
    headers = {
        'apikey': SUPABASE_KEY,
        'Authorization': f'Bearer {SUPABASE_KEY}',
        'Content-Type': 'image/jpeg',
    }
    resp = requests.post(url, data=file_bytes, headers=headers, timeout=30)
    if resp.status_code in (200, 201):
        public_url = f'{SUPABASE_STOR}/object/public/inspection-photos/{filename}'
        return public_url
    else:
        print(f'Supabase upload error: {resp.status_code} {resp.text}')
        return None


def save_inspection_to_db(record):
    """Сохраняет запись осмотра в таблицу inspections."""
    url = f'{SUPABASE_REST}/inspections'
    headers = {**SUPABASE_HEADERS, 'Prefer': 'return=representation'}
    resp = requests.post(url, json=record, headers=headers, timeout=15)
    if resp.status_code in (200, 201):
        return True
    else:
        print(f'Supabase DB error: {resp.status_code} {resp.text}')
        return False


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

        mileage_str = f"{int(mileage):,}".replace(',', ' ') + ' км' if mileage else '—'

        # Уникальный ID осмотра
        inspection_id = str(uuid.uuid4())
        safe_gos = gosnomer.replace('/', '-').replace('\\', '-')

        # ===== 1. Загружаем фото в Supabase Storage =====
        photo_urls = {}
        photo_bytes = {}

        for zone in ZONE_ORDER:
            if zone not in request.files:
                continue
            file = request.files[zone]
            if not file or not file.filename:
                continue
            file_bytes = file.read()
            photo_bytes[zone] = file_bytes

            filename = f'{safe_gos}/{inspection_id}/{zone}.jpg'
            url = upload_photo_to_supabase(file_bytes, filename)
            if url:
                photo_urls[zone] = url

        # ===== 2. Сохраняем запись в БД =====
        db_record = {
            'id':              inspection_id,
            'fio':             fio,
            'gosnomer':        gosnomer,
            'type':            type_,
            'inspection_date': date,
            'mileage':         mileage,
            'photo_front':     photo_urls.get('front'),
            'photo_back':      photo_urls.get('back'),
            'photo_left':      photo_urls.get('left'),
            'photo_right':     photo_urls.get('right'),
            'photo_salon':     photo_urls.get('salon'),
            'photo_sleep':     photo_urls.get('sleep'),
        }
        db_saved = save_inspection_to_db(db_record)

        # ===== 3. Отправляем в Telegram =====
        caption = (
            f"🚛 *ОСМОТР АВТОМОБИЛЯ*\n\n"
            f"👤 {fio}\n"
            f"🔢 {gosnomer}\n"
            f"📋 {type_.upper()}\n"
            f"📅 {date}\n"
            f"🛣 Пробег: {mileage_str}"
        )
        if db_saved:
            caption += f"\n✅ _Сохранено в базе данных_"

        media = []
        files_dict = {}

        for i, zone in enumerate(ZONE_ORDER):
            if zone not in photo_bytes:
                continue
            attach_name = f'photo_{zone}'
            files_dict[attach_name] = (f'{zone}.jpg', photo_bytes[zone], 'image/jpeg')
            media_item = {
                'type': 'photo',
                'media': f'attach://{attach_name}',
            }
            if i == 0:
                media_item['caption'] = caption
                media_item['parse_mode'] = 'Markdown'
            media.append(media_item)

        if not media:
            return jsonify({'ok': False, 'error': 'Нет фотографий'}), 400

        tg_data = {
            'chat_id': CHAT_ID,
            'media': json.dumps(media)
        }

        resp = requests.post(
            f'{TELEGRAM_API}/sendMediaGroup',
            data=tg_data,
            files=files_dict,
            timeout=60
        )

        tg_result = resp.json()
        if tg_result.get('ok'):
            return jsonify({'ok': True, 'db_saved': db_saved})
        else:
            print('Telegram error:', tg_result)
            return jsonify({'ok': False, 'error': 'Ошибка Telegram: ' + str(tg_result.get('description', ''))}), 500

    except Exception as e:
        print('Server error:', e)
        import traceback
        traceback.print_exc()
        return jsonify({'ok': False, 'error': str(e)}), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
