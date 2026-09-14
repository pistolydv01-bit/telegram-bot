import os,sqlite3
from functools import wraps
from flask import Flask,request,jsonify,session,send_from_directory,redirect

app=Flask(__name__,static_folder='admin',static_url_path='')
app.secret_key=os.environ.get('SESSION_SECRET','change-this-in-render')
ADMIN_EMAIL=os.environ.get('ADMIN_EMAIL')
ADMIN_PASSWORD=os.environ.get('ADMIN_PASSWORD')
DB=os.environ.get('DB_PATH','data.db')

def db():
    c=sqlite3.connect(DB); c.row_factory=sqlite3.Row
    c.execute('CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT, device TEXT, online INTEGER DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP)')
    c.execute('CREATE TABLE IF NOT EXISTS images(id INTEGER PRIMARY KEY, user_id INTEGER, filename TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP)')
    c.commit(); return c

def auth(f):
    @wraps(f)
    def w(*a,**k):
        if not session.get('admin'): return jsonify(error='Unauthorized'),401
        return f(*a,**k)
    return w

@app.get('/')
def home(): return send_from_directory('admin','index.html')

@app.post('/api/login')
def login():
    d=request.get_json(silent=True) or {}
    if not ADMIN_EMAIL or not ADMIN_PASSWORD: return jsonify(error='Render environment variables are not configured'),500
    if d.get('email')==ADMIN_EMAIL and d.get('password')==ADMIN_PASSWORD:
        session['admin']=True; return jsonify(ok=True)
    return jsonify(error='Invalid login details'),401

@app.post('/api/logout')
def logout(): session.clear(); return jsonify(ok=True)

@app.get('/dashboard')
@auth
def dashboard():
    return '<h1>Sun.DP Admin Dashboard</h1><p>Next: Users → Images/Status</p><p><a href="/">Logout/Login</a></p>'

@app.get('/api/users')
@auth
def users():
    c=db(); rows=[dict(x) for x in c.execute('SELECT * FROM users ORDER BY id DESC')]; c.close(); return jsonify(rows)

@app.get('/api/images')
@auth
def images():
    c=db(); rows=[dict(x) for x in c.execute('SELECT * FROM images ORDER BY id DESC')]; c.close(); return jsonify(rows)

@app.get('/api/status')
@auth
def status(): return jsonify(ok=True,service='admin',message='Backend online')

@app.get('/health')
def health(): return 'OK',200

if __name__=='__main__':
    db()
    app.run(host='0.0.0.0',port=int(os.environ.get('PORT','10000')))
