# SISEM Project - Security Best Practices Guide

## 🔐 Critical Security Issues Addressed

### 1. Credentials Management ✅
**BEFORE:**
```python
user = 'admin'
pws = '1234567890'
sisem.secret_key = '12345678900987654321@'
```

**AFTER:**
```python
user = os.environ.get('DEFAULT_USER', 'admin')
pws = os.environ.get('DEFAULT_PASSWORD', 'changeme')
sisem.secret_key = os.environ.get('SECRET_KEY', 'dev-key')
```

**Action**: Create `.env` file with strong credentials
```bash
SECRET_KEY=use-python-secrets-module
DEFAULT_USER=your_username
DEFAULT_PASSWORD=very_strong_password
```

---

## 🛡️ Remaining Security Tasks

### High Priority (Complete Before Production)

#### 1. Replace Hardcoded Authentication
```python
# TODO: Implement with database
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash

# Example:
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password = db.Column(db.String(255), nullable=False)
    
    def set_password(self, password):
        self.password = generate_password_hash(password)
    
    def check_password(self, password):
        return check_password_hash(self.password, password)
```

#### 2. Add CSRF Protection
```python
from flask_wtf.csrf import CSRFProtect

csrf = CSRFProtect(sisem)

# In templates:
# <form method="POST">
#     {{ csrf_token() }}
#     ...
# </form>
```

#### 3. Input Validation & Sanitization
```python
from flask_wtf import FlaskForm
from wtforms import StringField, validators

class MaintenanceForm(FlaskForm):
    equipment_type = StringField('Equipo', [
        validators.Length(min=1, max=100)
    ])
    serial_number = StringField('Serial', [
        validators.Regexp(r'^[A-Za-z0-9\-]+$')
    ])
```

#### 4. SQL Injection Prevention
```python
# NEVER do this:
query = f"SELECT * FROM users WHERE username = '{username}'"

# DO THIS instead:
from sqlalchemy import text
query = text("SELECT * FROM users WHERE username = :username")
db.session.execute(query, {"username": username})
```

#### 5. Add Rate Limiting
```python
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

limiter = Limiter(
    sisem,
    key_func=get_remote_address,
    default_limits=["200 per day", "50 per hour"]
)

@sisem.route('/login', methods=['POST'])
@limiter.limit("5 per minute")
def login():
    ...
```

---

### Medium Priority (Within 1-2 Sprints)

#### 1. Implement Proper Logging
```python
import logging
from logging.handlers import RotatingFileHandler

if not sisem.debug:
    file_handler = RotatingFileHandler('sisem.log', maxBytes=10240, backupCount=10)
    file_handler.setFormatter(logging.Formatter(
        '%(asctime)s %(levelname)s: %(message)s [in %(pathname)s:%(lineno)d]'
    ))
    file_handler.setLevel(logging.INFO)
    sisem.logger.addHandler(file_handler)
```

#### 2. Implement Session Management
```python
from datetime import timedelta

sisem.config['PERMANENT_SESSION_LIFETIME'] = timedelta(minutes=15)
sisem.config['SESSION_REFRESH_EACH_REQUEST'] = True

# Warn user before session timeout (client-side)
```

#### 3. Add HTTPS Enforcement
```python
# In production with Nginx/Apache:
# - Redirect HTTP to HTTPS
# - Use SSL certificates (Let's Encrypt)
# - Set HSTS headers

# Or in Flask:
@sisem.before_request
def enforce_https():
    if not request.is_secure and not app.debug:
        return redirect(request.url.replace('http://', 'https://', 1))
```

#### 4. File Upload Security
```python
import os
from werkzeug.utils import secure_filename

ALLOWED_EXTENSIONS = {'json', 'pdf', 'txt'}
MAX_CONTENT_LENGTH = 16 * 1024 * 1024  # 16MB

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

@sisem.route('/upload', methods=['POST'])
def upload_file():
    file = request.files['file']
    if file and allowed_file(file.filename):
        filename = secure_filename(file.filename)
        # Save securely...
```

---

## 🔍 Security Checklist for Production

- [ ] Remove all debug code
- [ ] Set `FLASK_DEBUG=False`
- [ ] Use strong SECRET_KEY (32+ characters, random)
- [ ] Implement database authentication
- [ ] Add CSRF protection
- [ ] Validate all inputs server-side
- [ ] Use HTTPS only
- [ ] Add rate limiting
- [ ] Implement proper logging
- [ ] Set up security headers
- [ ] Regular security audits
- [ ] Keep dependencies updated
- [ ] Use environment variables for secrets
- [ ] Never commit `.env` to version control
- [ ] Implement backup strategy
- [ ] Add database encryption
- [ ] Monitor for suspicious activity
- [ ] Implement API versioning
- [ ] Add API authentication tokens
- [ ] Document security procedures

---

## 📚 Recommended Libraries

```bash
# Authentication & Security
pip install Flask-Login flask-principal

# CSRF Protection
pip install Flask-WTF

# Database
pip install Flask-SQLAlchemy

# Environment variables
pip install python-dotenv

# Rate Limiting
pip install Flask-Limiter

# Security headers
pip install Flask-Talisman

# Form validation
pip install WTForms

# Password hashing
pip install bcrypt

# API documentation
pip install flask-restx

# Testing
pip install pytest pytest-cov
```

---

## 🚨 Emergency Security Procedures

### If Credentials Compromised:
1. Change SECRET_KEY immediately
2. Generate new password hashes for all users
3. Invalidate all sessions
4. Review access logs
5. Rotate database credentials
6. Update environment variables

### If Code Breach:
1. Scan codebase for hardcoded secrets
2. Invalidate all security tokens
3. Update authentication mechanisms
4. Audit database access
5. Notify users if necessary

---

## 📖 Resources

- [OWASP Top 10](https://owasp.org/www-project-top-ten/)
- [Flask Security](https://flask.palletsprojects.com/en/security/)
- [Python Security Best Practices](https://python.readthedocs.io/en/stable/library/security_warnings.html)
- [CWE Top 25](https://cwe.mitre.org/top25/)

