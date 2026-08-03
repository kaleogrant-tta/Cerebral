import os
import json
import base64
from flask import Flask, jsonify, request
from flask_cors import CORS
from google.oauth2 import service_account
from googleapiclient.discovery import build

app = Flask(__name__)
CORS(app)

SHEET_ID = '1lW2DOT0jwcB63YnhSuNHhKDo8bUotl__aLlmBemhOGA'
RANGE = 'Sheet1'
SCOPES = ['https://www.googleapis.com/auth/spreadsheets']

def get_service():
    creds_b64 = os.environ.get('GOOGLE_CREDENTIALS_B64')
    creds_dict = json.loads(base64.b64decode(creds_b64).decode('utf-8'))
    creds = service_account.Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    return build('sheets', 'v4', credentials=creds)

@app.route('/data', methods=['GET'])
def get_data():
    try:
        service = get_service()
        result = service.spreadsheets().values().get(
            spreadsheetId=SHEET_ID,
            range=RANGE
        ).execute()
        rows = result.get('values', [])
        return jsonify(rows)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/save', methods=['POST'])
def save_row():
    try:
        data = request.get_json()
        row = [
            data.get('date', ''),
            data.get('netSales', ''),
            data.get('netSalesLastWeek', ''),
            data.get('atv', ''),
            data.get('atvLastWeek', ''),
            data.get('inStore', ''),
            data.get('delivery', ''),
            data.get('total', ''),
            data.get('ffCount', ''),
            data.get('shoutouts', ''),
            data.get('focus', ''),
            data.get('inventory', ''),
            data.get('cash', '')
        ]
        service = get_service()
        result = service.spreadsheets().values().get(
            spreadsheetId=SHEET_ID,
            range=RANGE
        ).execute()
        rows = result.get('values', [])
        row_index = None
        for i, r in enumerate(rows):
            if r and r[0] == data.get('date'):
                row_index = i + 1
                break
        if row_index:
            service.spreadsheets().values().update(
                spreadsheetId=SHEET_ID,
                range=f'Sheet1!A{row_index}',
                valueInputOption='RAW',
                body={'values': [row]}
            ).execute()
        else:
            service.spreadsheets().values().append(
                spreadsheetId=SHEET_ID,
                range=RANGE,
                valueInputOption='RAW',
                insertDataOption='INSERT_ROWS',
                body={'values': [row]}
            ).execute()
        return jsonify({'status': 'ok'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok'})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
