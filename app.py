import os
import sqlite3
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, flash
from werkzeug.security import generate_password_hash, check_password_hash
from groq import Groq

app = Flask(__name__)
# Best Practice: Fallback to a default only in local dev environment
app.secret_key = os.environ.get('SECRET_KEY', 'dev_secret_key_change_in_production')
DB_NAME = 'database.db'

# --- GROQ CLIENT SETUP ---
groq_api_key = os.environ.get("GROQ_API_KEY")

if not groq_api_key:
    print("WARNING: GROQ_API_KEY environment variable is not set.")

# Initialize the Groq client
client = Groq(api_key=groq_api_key)

# Recommended fast production model on Groq
GROQ_MODEL = "llama-3.3-70b-versatile"

# --- DATABASE SETUP ---
def get_db_connection():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user'
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')
    
    # Default Admin account
    cursor.execute("SELECT * FROM users WHERE username = 'admin'")
    if not cursor.fetchone():
        hashed_pw = generate_password_hash('adminpassword')
        cursor.execute("INSERT INTO users (username, password, role) VALUES ('admin', ?, 'admin')", (hashed_pw,))
        
    conn.commit()
    conn.close()

init_db()

# --- DECORATORS ---
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash('Please log in first.', 'warning')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if session.get('role') != 'admin':
            flash('Access denied. Admin privileges required.', 'danger')
            return redirect(url_for('user_dashboard'))
        return f(*args, **kwargs)
    return decorated_function

# --- AUTH ROUTES ---
@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('admin_dashboard') if session['role'] == 'admin' else url_for('user_dashboard'))
    return redirect(url_for('login'))

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        username = request.form['username'].strip()
        password = request.form['password']
        role = request.form.get('role', 'user')

        if not username or not password:
            flash('All fields are required.', 'danger')
            return redirect(url_for('signup'))

        hashed_password = generate_password_hash(password)
        conn = get_db_connection()
        try:
            conn.execute('INSERT INTO users (username, password, role) VALUES (?, ?, ?)',
                         (username, hashed_password, role))
            conn.commit()
            flash('Registration successful! Please log in.', 'success')
            return redirect(url_for('login'))
        except sqlite3.IntegrityError:
            flash('Username already exists.', 'danger')
        finally:
            conn.close()
            
    return render_template('signup.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username'].strip()
        password = request.form['password']

        conn = get_db_connection()
        user = conn.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
        conn.close()

        if user and check_password_hash(user['password'], password):
            session['user_id'] = user['id']
            session['username'] = user['username']
            session['role'] = user['role']
            flash('Logged in successfully!', 'success')
            
            if user['role'] == 'admin':
                return redirect(url_for('admin_dashboard'))
            return redirect(url_for('user_dashboard'))
        else:
            flash('Invalid username or password.', 'danger')

    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    flash('You have been logged out.', 'info')
    return redirect(url_for('login'))

# --- USER ROUTE (PROMPT -> GROQ API -> SAVE RESPONSE) ---
@app.route('/user/dashboard', methods=['GET', 'POST'])
@login_required
def user_dashboard():
    conn = get_db_connection()
    try:
        if request.method == 'POST':
            prompt = request.form.get('prompt', '').strip()
            
            if prompt:
                try:
                    # Call Groq Chat Completions API
                    response = client.chat.completions.create(
                        messages=[
                            {"role": "system", "content": "You are a helpful and concise AI assistant."},
                            {"role": "user", "content": prompt}
                        ],
                        model=GROQ_MODEL,
                        max_tokens=500,
                        temperature=0.7
                    )
                    
                    if response and response.choices and len(response.choices) > 0:
                        ai_answer = response.choices[0].message.content
                    else:
                        ai_answer = "No response text generated."

                    conn.execute(
                        'INSERT INTO user_data (user_id, title, content) VALUES (?, ?, ?)',
                        (session['user_id'], prompt, ai_answer)
                    )
                    conn.commit()
                    flash('AI generated an answer and saved your prompt!', 'success')
                except Exception as e:
                    flash(f'Groq API Error: {str(e)}', 'danger')
            else:
                flash('Prompt cannot be empty.', 'danger')

        user_entries = conn.execute(
            'SELECT * FROM user_data WHERE user_id = ? ORDER BY created_at DESC', 
            (session['user_id'],)
        ).fetchall()
    finally:
        conn.close()

    return render_template('user_dashboard.html', entries=user_entries)

# --- ADMIN CRUD ROUTES ---
@app.route('/admin/dashboard', methods=['GET', 'POST'])
@login_required
@admin_required
def admin_dashboard():
    conn = get_db_connection()
    
    if request.method == 'POST':
        prompt = request.form.get('title', '').strip()
        content = request.form.get('content', '').strip()
        if prompt and content:
            conn.execute('INSERT INTO user_data (user_id, title, content) VALUES (?, ?, ?)',
                         (session['user_id'], prompt, content))
            conn.commit()
            flash('Admin record created!', 'success')

    entries = conn.execute('''
        SELECT user_data.id, user_data.title, user_data.content, user_data.created_at, users.username 
        FROM user_data 
        JOIN users ON user_data.user_id = users.id 
        ORDER BY user_data.created_at DESC
    ''').fetchall()
    conn.close()

    return render_template('admin_dashboard.html', entries=entries)

@app.route('/admin/edit/<int:entry_id>', methods=['GET', 'POST'])
@login_required
@admin_required
def admin_edit(entry_id):
    conn = get_db_connection()
    entry = conn.execute('SELECT * FROM user_data WHERE id = ?', (entry_id,)).fetchone()

    if not entry:
        conn.close()
        flash('Entry not found.', 'danger')
        return redirect(url_for('admin_dashboard'))

    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        content = request.form.get('content', '').strip()

        if title and content:
            conn.execute('UPDATE user_data SET title = ?, content = ? WHERE id = ?',
                         (title, content, entry_id))
            conn.commit()
            conn.close()
            flash('Record updated successfully!', 'success')
            return redirect(url_for('admin_dashboard'))

    conn.close()
    return render_template('edit_entry.html', entry=entry)

@app.route('/admin/delete/<int:entry_id>', methods=['POST'])
@login_required
@admin_required
def admin_delete(entry_id):
    conn = get_db_connection()
    conn.execute('DELETE FROM user_data WHERE id = ?', (entry_id,))
    conn.commit()
    conn.close()
    flash('Record deleted successfully!', 'danger')
    return redirect(url_for('admin_dashboard'))

import os

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)