from flask import Flask, request, jsonify, send_file, render_template
from flask_cors import CORS
import os
import io
import json
from datetime import datetime
import mido
from mido import MidiFile, MidiTrack, Message, MetaMessage
import tempfile

app = Flask(__name__)
CORS(app)

# Konfigurace pro upload souborů
UPLOAD_FOLDER = 'uploads'
PROCESSED_FOLDER = 'processed'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['PROCESSED_FOLDER'] = PROCESSED_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max

# Vytvoření složek pokud neexistují
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(PROCESSED_FOLDER, exist_ok=True)
os.makedirs('templates', exist_ok=True)

def log_message(message, msg_type='info'):
    timestamp = datetime.now().strftime('%H:%M:%S')
    return f"[{timestamp}] {message}"

def write_var_int(value):
    """Převede číslo na variable length quantity"""
    if value == 0:
        return bytes([0x00])
    
    bytes_list = []
    while value > 0:
        byte = value & 0x7F
        value = value >> 7
        if bytes_list:
            byte |= 0x80
        bytes_list.insert(0, byte)
    
    return bytes(bytes_list)

def quantize_tick(tick, grid_size, start_tick=0, mode='nearest', strength=1.0):
    """Kvantizuje jednotlivý tick podle zadaných parametrů"""
    relative_tick = tick - start_tick
    grid_position = relative_tick / grid_size
    
    if mode == 'forward':
        target_grid = int(grid_position) + (1 if grid_position % 1 > 0 else 0)
    elif mode == 'backward':
        target_grid = int(grid_position)
    else:  # nearest
        target_grid = round(grid_position)
    
    target_tick = start_tick + (target_grid * grid_size)
    shift = target_tick - tick
    final_shift = int(shift * strength)
    
    return tick + final_shift

def prevent_note_overlaps(notes, min_gap=3):
    """Zabrání překryvům not s minimální mezerou"""
    notes.sort(key=lambda x: x['quantized_on'])
    
    for i in range(len(notes) - 1):
        current = notes[i]
        next_note = notes[i + 1]
        
        required_end = next_note['quantized_on'] - min_gap
        if current['quantized_off'] > required_end:
            current['quantized_off'] = max(required_end, current['quantized_on'] + 1)
    
    return notes

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/analyze_midi', methods=['POST'])
def analyze_midi():
    """Analyzuje nahraný MIDI soubor a vrátí informace o notách"""
    try:
        if 'file' not in request.files:
            return jsonify({'error': 'Žádný soubor nebyl nahrán'}), 400
        
        file = request.files['file']
        if file.filename == '':
            return jsonify({'error': 'Nebyl vybrán žádný soubor'}), 400
        
        # Uložení souboru
        filename = file.filename
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(filepath)
        
        # Analýza MIDI souboru
        midi_file = MidiFile(filepath)
        
        notes = []
        tempo = 500000  # Default tempo (120 BPM)
        time_signature = (4, 4)
        
        # Získání informací o tempu a taktu
        for track in midi_file.tracks:
            for msg in track:
                if msg.type == 'set_tempo':
                    tempo = msg.tempo
                elif msg.type == 'time_signature':
                    time_signature = (msg.numerator, msg.denominator)
        
        # Extrakce not
        for track_idx, track in enumerate(midi_file.tracks):
            current_time = 0
            active_notes = {}
            
            for msg in track:
                current_time += msg.time
                
                if msg.type == 'note_on' and msg.velocity > 0:
                    active_notes[msg.note] = {
                        'on_time': current_time,
                        'note': msg.note,
                        'velocity': msg.velocity,
                        'track': track_idx
                    }
                elif msg.type == 'note_off' or (msg.type == 'note_on' and msg.velocity == 0):
                    if msg.note in active_notes:
                        note_data = active_notes.pop(msg.note)
                        notes.append({
                            'original_on': note_data['on_time'],
                            'original_off': current_time,
                            'note': note_data['note'],
                            'velocity': note_data['velocity'],
                            'track': note_data['track'],
                            'duration': current_time - note_data['on_time']
                        })
        
        # Seřazení not podle času
        notes.sort(key=lambda x: x['original_on'])
        
        response_data = {
            'filename': filename,
            'format': midi_file.type,
            'tracks': len(midi_file.tracks),
            'ticks_per_beat': midi_file.ticks_per_beat,
            'tempo': tempo,
            'time_signature': time_signature,
            'notes': notes,
            'note_count': len(notes),
            'logs': [log_message(f'MIDI soubor analyzován: {len(notes)} not nalezeno')]
        }
        
        return jsonify(response_data)
        
    except Exception as e:
        return jsonify({'error': f'Chyba při analýze MIDI: {str(e)}'}), 500

@app.route('/api/quantize', methods=['POST'])
def quantize_notes():
    """Kvantizuje noty podle zadaných parametrů"""
    try:
        data = request.get_json()
        
        notes = data.get('notes', [])
        grid_size = data.get('grid_size', 15)
        start_tick = data.get('start_tick', 0)
        mode = data.get('mode', 'nearest')
        strength = data.get('strength', 100) / 100.0
        min_gap = data.get('min_gap', 3)
        
        if not notes:
            return jsonify({'error': 'Nebyly zadány žádné noty'}), 400
        
        logs = []
        logs.append(log_message(f'Začínám kvantizaci {len(notes)} not'))
        logs.append(log_message(f'Parametry: grid={grid_size}, start={start_tick}, mode={mode}, strength={strength*100}%'))
        
        # Kvantizace každé noty
        quantized_notes = []
        total_shift = 0
        
        for i, note in enumerate(notes):
            original_on = note['original_on']
            original_off = note['original_off']
            duration = note['duration']
            
            # Kvantizace začátku noty
            quantized_on = quantize_tick(original_on, grid_size, start_tick, mode, strength)
            quantized_off = quantized_on + duration
            
            shift = abs(quantized_on - original_on)
            total_shift += shift
            
            quantized_note = note.copy()
            quantized_note.update({
                'quantized_on': quantized_on,
                'quantized_off': quantized_off,
                'shift': quantized_on - original_on
            })
            
            quantized_notes.append(quantized_note)
            
            logs.append(log_message(f'Nota {i+1}: {original_on} → {quantized_on} (posun: {quantized_note["shift"]})'))
        
        # Prevence překryvů
        if min_gap > 0:
            logs.append(log_message(f'Kontrola překryvů s minimální mezerou {min_gap} ticků'))
            quantized_notes = prevent_note_overlaps(quantized_notes, min_gap)
        
        # Statistiky
        avg_shift = total_shift / len(quantized_notes) if quantized_notes else 0
        
        logs.append(log_message('Kvantizace dokončena!'))
        
        return jsonify({
            'quantized_notes': quantized_notes,
            'stats': {
                'note_count': len(quantized_notes),
                'total_shift': int(total_shift),
                'avg_shift': round(avg_shift, 1)
            },
            'logs': logs
        })
        
    except Exception as e:
        return jsonify({'error': f'Chyba při kvantizaci: {str(e)}'}), 500

@app.route('/api/export_midi', methods=['POST'])
def export_midi():
    """Exportuje kvantizované noty jako MIDI soubor"""
    try:
        data = request.get_json()
        
        original_filename = data.get('filename', 'input.mid')
        quantized_notes = data.get('quantized_notes', [])
        ticks_per_beat = data.get('ticks_per_beat', 480)
        tempo = data.get('tempo', 500000)
        time_signature = data.get('time_signature', [4, 4])
        
        if not quantized_notes:
            return jsonify({'error': 'Žádné noty k exportu'}), 400
        
        # Vytvoření nového MIDI souboru
        mid = MidiFile(ticks_per_beat=ticks_per_beat)
        track = MidiTrack()
        mid.tracks.append(track)
        
        # Přidání meta událostí
        track.append(MetaMessage('set_tempo', tempo=tempo, time=0))
        track.append(MetaMessage('time_signature', 
                               numerator=time_signature[0], 
                               denominator=time_signature[1], 
                               time=0))
        
        # Seřazení not podle kvantizovaného času
        quantized_notes.sort(key=lambda x: x['quantized_on'])
        
        # Vytvoření seznamu všech MIDI událostí
        events = []
        
        for note in quantized_notes:
            # Note On událost
            events.append({
                'time': note['quantized_on'],
                'type': 'note_on',
                'note': note['note'],
                'velocity': note['velocity']
            })
            
            # Note Off událost
            events.append({
                'time': note['quantized_off'],
                'type': 'note_off',
                'note': note['note'],
                'velocity': 0
            })
        
        # Seřazení všech událostí podle času
        events.sort(key=lambda x: x['time'])
        
        # Přidání událostí do tracku s delta time
        last_time = 0
        for event in events:
            delta_time = event['time'] - last_time
            
            if event['type'] == 'note_on':
                track.append(Message('note_on', 
                                   note=event['note'], 
                                   velocity=event['velocity'], 
                                   time=delta_time))
            else:
                track.append(Message('note_off', 
                                   note=event['note'], 
                                   velocity=event['velocity'], 
                                   time=delta_time))
            
            last_time = event['time']
        
        # Uložení do dočasného souboru
        output_filename = original_filename.replace('.mid', '_quantized.mid').replace('.midi', '_quantized.mid')
        if not output_filename.endswith('.mid'):
            output_filename += '_quantized.mid'
        
        output_path = os.path.join(app.config['PROCESSED_FOLDER'], output_filename)
        mid.save(output_path)
        
        return jsonify({
            'success': True,
            'filename': output_filename,
            'message': 'MIDI soubor byl úspěšně vytvořen',
            'download_url': f'/api/download/{output_filename}'
        })
        
    except Exception as e:
        return jsonify({'error': f'Chyba při exportu MIDI: {str(e)}'}), 500

@app.route('/api/download/<filename>')
def download_file(filename):
    """Stažení zpracovaného MIDI souboru"""
    try:
        file_path = os.path.join(app.config['PROCESSED_FOLDER'], filename)
        if os.path.exists(file_path):
            return send_file(file_path, as_attachment=True, download_name=filename)
        else:
            return jsonify({'error': 'Soubor nenalezen'}), 404
    except Exception as e:
        return jsonify({'error': f'Chyba při stahování: {str(e)}'}), 500

@app.route('/api/add_sample', methods=['POST'])
def add_sample_notes():
    """Přidá vzorové noty pro testování"""
    sample_notes = [
        {'original_on': 44, 'original_off': 104, 'note': 60, 'velocity': 100, 'track': 0, 'duration': 60},
        {'original_on': 376, 'original_off': 436, 'note': 62, 'velocity': 100, 'track': 0, 'duration': 60},
        {'original_on': 433, 'original_off': 493, 'note': 64, 'velocity': 100, 'track': 0, 'duration': 60},
        {'original_on': 487, 'original_off': 547, 'note': 65, 'velocity': 100, 'track': 0, 'duration': 60},
        {'original_on': 533, 'original_off': 593, 'note': 67, 'velocity': 100, 'track': 0, 'duration': 60},
        {'original_on': 600, 'original_off': 660, 'note': 69, 'velocity': 100, 'track': 0, 'duration': 60}
    ]
    
    return jsonify({
        'notes': sample_notes,
        'filename': 'sample_notes.mid',
        'ticks_per_beat': 480,
        'tempo': 500000,
        'time_signature': [4, 4],
        'logs': [log_message('Přidány vzorové noty')]
    })

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)