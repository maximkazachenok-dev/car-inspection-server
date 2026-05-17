import os
import asyncio
from flask import Flask, request, jsonify
from flask_cors import CORS
import requests

app = Flask(__name__)
CORS(app)

# ======= НАСТРОЙКИ — ЗАМЕНИТЬ СВОИМИ =======
BOT_TOKEN = os.environ.get('BOT_TOKEN', 'ВАШ_ТОКЕН_БОТА')
CHAT_ID   = os.environ.get('CHAT_ID',   'ВАШ_CHAT_ID')
# ===========================================

TELEGRAM_API = f'https://api.telegram.org/bot{BOT_TOKEN}'

ZONE_LABELS = {
    'front': '📸 Спереди',
    'back':  '📸 Сзади',
    'left':  '📸 Левый бок',
    'right': '📸 Правый бок',
    'salon': '📸 Салон',
    'sleep': '📸 Спальник',
}

@app.route('/', methods=['GET'])
def health():
    return 'OK', 200

@app.route('/submit', methods=['POST'])
def submit():
    try:
        fio      = request.form.get('fio', '').strip()
        gosnomer = request.form.get('gosnomer', '').strip().upper()
        type_    = request.form.get('type', '').strip()
        date     = request.form.get('date', '').strip()

        if not all([fio, gosnomer, type_, date]):
            return jsonify({'ok': False, 'error': 'Не все поля заполнены'}), 400

        mileage  = request.form.get('mileage', '').strip()

        # Текстовое сообщение — заголовок акта
        mileage_str = f"{int(mileage):,}".replace(',', ' ') + ' км' if mileage else '—'
        caption = (
            f"🚛 *ОСМОТР АВТОМОБИЛЯ*\n\n"
            f"👤 {fio}\n"
            f"🔢 {gosnomer}\n"
            f"📋 {type_.upper()}\n"
            f"📅 {date}\n"
            f"🛣 Пробег: {mileage_str}"
        )

        # Собираем медиагруппу (до 10 фото)
        media = []
        files_dict = {}
        zone_order = ['front', 'back', 'left', 'right', 'salon', 'sleep']

        for i, zone in enumerate(zone_order):
            if zone not in request.files:
                continue
            file = request.files[zone]
            if not file or not file.filename:
                continue

            attach_name = f'photo_{zone}'
            files_dict[attach_name] = (f'{zone}.jpg', file.read(), 'image/jpeg')

            media_item = {
                'type': 'photo',
                'media': f'attach://{attach_name}',
            }
            # Подпись только к первому фото
            if i == 0:
                media_item['caption'] = caption
                media_item['parse_mode'] = 'Markdown'

            media.append(media_item)

        if not media:
            return jsonify({'ok': False, 'error': 'Нет фотографий'}), 400

        import json
        data = {
            'chat_id': CHAT_ID,
            'media': json.dumps(media)
        }

        resp = requests.post(
            f'{TELEGRAM_API}/sendMediaGroup',
            data=data,
            files=files_dict,
            timeout=60
        )

        tg_result = resp.json()
        if tg_result.get('ok'):
            return jsonify({'ok': True})
        else:
            print('Telegram error:', tg_result)
            return jsonify({'ok': False, 'error': 'Ошибка Telegram: ' + str(tg_result.get('description', ''))}), 500

    except Exception as e:
        print('Server error:', e)
        return jsonify({'ok': False, 'error': str(e)}), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
