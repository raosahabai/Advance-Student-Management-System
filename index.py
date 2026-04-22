#!/usr/bin/env python3
"""
UMS GUI App (fixed + migration) — single file, no external packages required.

This version:
- Adds automatic migration (adds missing columns to timetable if DB from older versions)
- Uses safe access to sqlite3.Row fields so missing keys won't crash the UI
- All other features remain as before (student token generation, teacher marking, delete, 12-hour times)

Save as ums_app_fixed_v2.py and run with Python 3 (IDLE).
"""

import tkinter as tk
from tkinter import ttk, messagebox, simpledialog, filedialog
import sqlite3
from datetime import datetime, timedelta
import secrets
import json
import urllib.parse
import urllib.request
import tempfile
import os
import csv

# ----- Config -----
DB_FILE = "ums_app_fixed.db"
QR_VALID_MINUTES = 10
QR_IMAGE_DIR = os.path.join(tempfile.gettempdir(), "ums_qr_images")
os.makedirs(QR_IMAGE_DIR, exist_ok=True)

# ----- DB helpers & migration -----
def get_conn():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def _table_columns(conn, table_name):
    cur = conn.execute(f"PRAGMA table_info({table_name})")
    return [r["name"] for r in cur.fetchall()]

def init_db():
    conn = get_conn()
    cur = conn.cursor()

    # Create tables if missing (base schema)
    cur.executescript("""
    CREATE TABLE IF NOT EXISTS student (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        student_id TEXT UNIQUE NOT NULL,
        name TEXT
    );
    CREATE TABLE IF NOT EXISTS teacher (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        teacher_id TEXT UNIQUE NOT NULL,
        name TEXT
    );
    CREATE TABLE IF NOT EXISTS course (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT NOT NULL,
        title TEXT
    );
    CREATE TABLE IF NOT EXISTS timetable (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        course_id INTEGER NOT NULL,
        teacher_id INTEGER NOT NULL,
        date TEXT,
        start_time TEXT NOT NULL,
        duration_minutes INTEGER NOT NULL DEFAULT 60,
        token TEXT,
        token_generated_at TEXT,
        FOREIGN KEY(course_id) REFERENCES course(id),
        FOREIGN KEY(teacher_id) REFERENCES teacher(id)
    );
    CREATE TABLE IF NOT EXISTS student_tokens (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        student_id INTEGER NOT NULL,
        timetable_id INTEGER NOT NULL,
        token TEXT UNIQUE NOT NULL,
        generated_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        used INTEGER NOT NULL DEFAULT 0,
        FOREIGN KEY(student_id) REFERENCES student(id),
        FOREIGN KEY(timetable_id) REFERENCES timetable(id)
    );
    CREATE TABLE IF NOT EXISTS attendance (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        student_id INTEGER NOT NULL,
        timetable_id INTEGER NOT NULL,
        timestamp TEXT NOT NULL,
        FOREIGN KEY(student_id) REFERENCES student(id),
        FOREIGN KEY(timetable_id) REFERENCES timetable(id),
        UNIQUE(student_id, timetable_id)
    );
    """)
    conn.commit()

    # Migration: add optional columns to timetable if they do not exist
    existing = _table_columns(conn, "timetable")
    needed = {
        "day_of_week": "TEXT",
        "batch": "TEXT",
        "section": "TEXT"
    }
    for col, col_type in needed.items():
        if col not in existing:
            try:
                cur.execute(f"ALTER TABLE timetable ADD COLUMN {col} {col_type}")
                conn.commit()
            except Exception:
                # ignore if cannot alter (some SQLite builds), proceed safely
                pass

    conn.close()

# ----- Safe accessor helper -----
def safe(row, key, default=""):
    """Return row[key] if exists else default. row is sqlite3.Row."""
    if row is None:
        return default
    try:
        if key in row.keys():
            return row[key]
        return default
    except Exception:
        return default

# ----- CRUD helpers -----
def add_student_db(student_id, name):
    conn = get_conn()
    try:
        conn.execute("INSERT INTO student (student_id, name) VALUES (?, ?)", (student_id, name))
        conn.commit()
        return True, "Student added"
    except sqlite3.IntegrityError:
        return False, "Student ID already exists"
    finally:
        conn.close()

def list_students_db():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM student ORDER BY student_id").fetchall()
    conn.close()
    return rows

def delete_student_db(student_row_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM student WHERE id = ?", (student_row_id,))
    cur.execute("DELETE FROM student_tokens WHERE student_id = ?", (student_row_id,))
    cur.execute("DELETE FROM attendance WHERE student_id = ?", (student_row_id,))
    conn.commit()
    conn.close()

def add_teacher_db(teacher_id, name):
    conn = get_conn()
    try:
        conn.execute("INSERT INTO teacher (teacher_id, name) VALUES (?, ?)", (teacher_id, name))
        conn.commit()
        return True, "Teacher added"
    except sqlite3.IntegrityError:
        return False, "Teacher ID exists"
    finally:
        conn.close()

def list_teachers_db():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM teacher ORDER BY teacher_id").fetchall()
    conn.close()
    return rows

def delete_teacher_db(teacher_row_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM teacher WHERE id = ?", (teacher_row_id,))
    cur.execute("DELETE FROM timetable WHERE teacher_id = ?", (teacher_row_id,))
    conn.commit()
    conn.close()

def add_course_db(code, title):
    conn = get_conn()
    conn.execute("INSERT INTO course (code, title) VALUES (?, ?)", (code, title))
    conn.commit()
    conn.close()

def list_courses_db():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM course ORDER BY code").fetchall()
    conn.close()
    return rows

def delete_course_db(course_row_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM course WHERE id = ?", (course_row_id,))
    cur.execute("DELETE FROM timetable WHERE course_id = ?", (course_row_id,))
    conn.commit()
    conn.close()

def create_timetable_db(course_id, teacher_id, date_s, day_of_week, start_time_24, duration, batch, section):
    conn = get_conn()
    conn.execute("""
        INSERT INTO timetable (course_id, teacher_id, date, day_of_week, start_time, duration_minutes, batch, section)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (course_id, teacher_id, date_s if date_s else None, day_of_week if day_of_week else None, start_time_24, duration, batch, section))
    conn.commit()
    conn.close()

def list_timetables_db():
    conn = get_conn()
    rows = conn.execute("""
        SELECT t.*, c.code as course_code, c.title as course_title,
               tr.teacher_id as teacher_code, tr.name as teacher_name
        FROM timetable t
        JOIN course c ON c.id = t.course_id
        JOIN teacher tr ON tr.id = t.teacher_id
        ORDER BY COALESCE(t.date, '9999-12-31') DESC, t.start_time
    """).fetchall()
    conn.close()
    return rows

def delete_timetable_db(tt_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM timetable WHERE id = ?", (tt_id,))
    cur.execute("DELETE FROM student_tokens WHERE timetable_id = ?", (tt_id,))
    cur.execute("DELETE FROM attendance WHERE timetable_id = ?", (tt_id,))
    conn.commit()
    conn.close()

# ----- Token & Attendance logic -----
def student_generate_token_db(student_identifier, tt_id):
    conn = get_conn()
    cur = conn.cursor()
    student = cur.execute("SELECT * FROM student WHERE student_id = ?", (student_identifier,)).fetchone()
    if not student:
        conn.close()
        return False, "Student not found"
    tt = cur.execute("SELECT t.*, c.code as course_code FROM timetable t JOIN course c ON c.id = t.course_id WHERE t.id = ?", (tt_id,)).fetchone()
    if not tt:
        conn.close()
        return False, "Timetable entry not found"

    # Determine the session start datetime
    if safe(tt, 'date', None):
        start_dt = datetime.fromisoformat(safe(tt, 'date') + "T" + safe(tt, 'start_time'))
    else:
        # day_of_week case: allow only on that weekday
        dow = safe(tt, 'day_of_week', '')
        today_name = datetime.now().strftime("%A")
        if dow != today_name:
            conn.close()
            return False, f"This timetable is for {dow}. Today is {today_name}."
        start_dt = datetime.fromisoformat(datetime.now().date().isoformat() + "T" + safe(tt, 'start_time'))

    expiry_dt = start_dt + timedelta(minutes=QR_VALID_MINUTES)
    now = datetime.now()
    if now > expiry_dt:
        conn.close()
        return False, "Session already expired"

    token = secrets.token_urlsafe(12)
    generated_at = now.isoformat()
    expires_at = expiry_dt.isoformat()
    try:
        cur.execute("INSERT INTO student_tokens (student_id, timetable_id, token, generated_at, expires_at, used) VALUES (?, ?, ?, ?, ?, 0)",
                    (student['id'], tt_id, token, generated_at, expires_at))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return False, "Token conflict — try again"
    conn.close()
    payload = {
        "timetable_id": tt_id,
        "course_code": safe(tt, 'course_code'),
        "student_id": student_identifier,
        "token": token,
        "expires_at": expires_at
    }
    return True, payload

def teacher_mark_by_token_db(token_input):
    conn = get_conn()
    cur = conn.cursor()
    token_row = cur.execute("""
        SELECT st.*, s.student_id as student_code, t.start_time, t.date, t.day_of_week, t.id as timetable_id
        FROM student_tokens st
        JOIN student s ON s.id = st.student_id
        JOIN timetable t ON t.id = st.timetable_id
        WHERE st.token = ?
    """, (token_input,)).fetchone()
    if not token_row:
        conn.close()
        return False, "Invalid token"
    if token_row['used']:
        conn.close()
        return False, "Token already used"
    now = datetime.now()
    expires_at = datetime.fromisoformat(token_row['expires_at'])
    if now > expires_at:
        conn.close()
        return False, "Token expired"
    # ensure class has started (don't mark too early)
    if token_row['date']:
        start_dt = datetime.fromisoformat(token_row['date'] + "T" + token_row['start_time'])
    else:
        start_dt = datetime.fromisoformat(datetime.now().date().isoformat() + "T" + token_row['start_time'])
    if now < start_dt:
        conn.close()
        return False, f"Class hasn't started yet (starts at {start_dt})"
    # check duplicate
    exists = cur.execute("SELECT * FROM attendance WHERE student_id = ? AND timetable_id = ?", (token_row['student_id'], token_row['timetable_id'])).fetchone()
    if exists:
        cur.execute("UPDATE student_tokens SET used = 1 WHERE id = ?", (token_row['id'],))
        conn.commit()
        conn.close()
        return False, "Attendance already recorded for this student for this session"
    # record
    ts = now.isoformat()
    cur.execute("INSERT INTO attendance (student_id, timetable_id, timestamp) VALUES (?, ?, ?)", (token_row['student_id'], token_row['timetable_id'], ts))
    cur.execute("UPDATE student_tokens SET used = 1 WHERE id = ?", (token_row['id'],))
    conn.commit()
    conn.close()
    return True, f"Attendance recorded for {token_row['student_code']}"

# ----- Attendance view/export -----
def view_attendance_for_timetable_db(tt_id):
    conn = get_conn()
    rows = conn.execute("""
        SELECT a.id, s.student_id, s.name, a.timestamp
        FROM attendance a
        JOIN student s ON s.id = a.student_id
        WHERE a.timetable_id = ?
        ORDER BY a.timestamp
    """, (tt_id,)).fetchall()
    conn.close()
    return rows

def export_attendance_csv_db(tt_id, filename):
    rows = view_attendance_for_timetable_db(tt_id)
    if not rows:
        return False, "No attendance to export"
    with open(filename, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['student_id','name','timestamp'])
        for r in rows:
            w.writerow([r['student_id'], r['name'], r['timestamp']])
    return True, f"Exported to {filename}"

# ----- QR fetch helper (optional) -----
def fetch_qr_image(payload_json, filename_path):
    data = urllib.parse.quote(payload_json, safe='')
    url = f"https://api.qrserver.com/v1/create-qr-code/?size=300x300&data={data}"
    try:
        urllib.request.urlretrieve(url, filename_path)
        return True, None
    except Exception as e:
        return False, str(e)

# ----- time helpers -----
def parse_time_12h_to_24h(s):
    try:
        dt = datetime.strptime(s.strip(), "%I:%M %p")
        return dt.strftime("%H:%M")
    except Exception:
        try:
            dt = datetime.strptime(s.strip(), "%I:%M%p")
            return dt.strftime("%H:%M")
        except Exception:
            raise ValueError("Bad time format. Use hh:mm AM/PM")

def format_time_24h_to_12h(hhmm):
    try:
        dt = datetime.strptime(hhmm, "%H:%M")
        return dt.strftime("%I:%M %p")
    except Exception:
        return hhmm

# ----- GUI Application -----
class UMSApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("UMS — QR Attendance (fixed v2)")
        self.geometry("1100x700")
        self._qr_photo = None
        self.create_widgets()
        self.refresh_all()

    def create_widgets(self):
        nb = ttk.Notebook(self)
        nb.pack(fill='both', expand=True, padx=6, pady=6)

        self.tab_manage = ttk.Frame(nb)
        self.tab_timetable = ttk.Frame(nb)
        self.tab_student = ttk.Frame(nb)
        self.tab_qr = ttk.Frame(nb)
        self.tab_att = ttk.Frame(nb)

        nb.add(self.tab_manage, text="Manage")
        nb.add(self.tab_timetable, text="Timetable / Teacher Mark")
        nb.add(self.tab_student, text="Student Portal")
        nb.add(self.tab_qr, text="QR / Last Payload")
        nb.add(self.tab_att, text="Attendance")

        self.build_manage_tab()
        self.build_timetable_tab()
        self.build_student_tab()
        self.build_qr_tab()
        self.build_att_tab()

    # ---- Manage tab ----
    def build_manage_tab(self):
        f = self.tab_manage
        f.columnconfigure(0, weight=1)
        f.columnconfigure(1, weight=1)

        sf = ttk.LabelFrame(f, text="Students")
        sf.grid(row=0, column=0, sticky='nsew', padx=6, pady=6)
        self.lb_students = tk.Listbox(sf, height=12)
        self.lb_students.pack(side='left', fill='both', expand=True, padx=6, pady=6)
        sf_btn = ttk.Frame(sf); sf_btn.pack(side='left', fill='y', padx=6, pady=6)
        ttk.Button(sf_btn, text="Add", command=self.ui_add_student).pack(fill='x', pady=2)
        ttk.Button(sf_btn, text="Delete", command=self.ui_delete_selected_student).pack(fill='x', pady=2)
        ttk.Button(sf_btn, text="Refresh", command=self.refresh_students_listbox).pack(fill='x', pady=2)

        tf = ttk.LabelFrame(f, text="Teachers")
        tf.grid(row=0, column=1, sticky='nsew', padx=6, pady=6)
        self.lb_teachers = tk.Listbox(tf, height=6)
        self.lb_teachers.pack(fill='both', padx=6, pady=6)
        tb = ttk.Frame(tf); tb.pack(fill='x', padx=6, pady=6)
        ttk.Button(tb, text="Add", command=self.ui_add_teacher).pack(side='left')
        ttk.Button(tb, text="Delete", command=self.ui_delete_selected_teacher).pack(side='left', padx=6)
        ttk.Button(tb, text="Refresh", command=self.refresh_teachers_listbox).pack(side='left', padx=6)

        cf = ttk.LabelFrame(f, text="Courses")
        cf.grid(row=1, column=1, sticky='nsew', padx=6, pady=6)
        self.lb_courses = tk.Listbox(cf, height=6)
        self.lb_courses.pack(fill='both', padx=6, pady=6)
        cb = ttk.Frame(cf); cb.pack(fill='x', padx=6, pady=6)
        ttk.Button(cb, text="Add", command=self.ui_add_course).pack(side='left')
        ttk.Button(cb, text="Delete", command=self.ui_delete_selected_course).pack(side='left', padx=6)
        ttk.Button(cb, text="Refresh", command=self.refresh_courses_listbox).pack(side='left', padx=6)

        self.refresh_students_listbox()
        self.refresh_teachers_listbox()
        self.refresh_courses_listbox()

    def ui_add_student(self):
        sid = simpledialog.askstring("Student ID", "Student ID (eg STU001):", parent=self)
        if not sid:
            return
        name = simpledialog.askstring("Name", "Student name (optional):", parent=self) or ""
        ok, msg = add_student_db(sid.strip(), name.strip())
        messagebox.showinfo("Add Student", msg)
        self.refresh_students_listbox()

    def ui_delete_selected_student(self):
        sel = self.lb_students.curselection()
        if not sel:
            messagebox.showinfo("Delete", "Select a student.")
            return
        line = self.lb_students.get(sel[0])
        student_code = line.split(" — ")[0]
        rows = list_students_db()
        row = next((r for r in rows if r['student_id'] == student_code), None)
        if not row:
            messagebox.showerror("Delete", "Student not found in DB.")
            return
        if not messagebox.askyesno("Confirm", f"Delete student {student_code}? This will remove tokens & attendance for this student."):
            return
        delete_student_db(row['id'])
        self.refresh_students_listbox()
        self.refresh_att_combo()

    def refresh_students_listbox(self):
        self.lb_students.delete(0, 'end')
        for r in list_students_db():
            self.lb_students.insert('end', f"{r['student_id']} — {r['name']}")

    def ui_add_teacher(self):
        tid = simpledialog.askstring("Teacher ID", "Teacher ID (eg TCH001):", parent=self)
        if not tid:
            return
        name = simpledialog.askstring("Name", "Teacher name (optional):", parent=self) or ""
        ok, msg = add_teacher_db(tid.strip(), name.strip())
        messagebox.showinfo("Add Teacher", msg)
        self.refresh_teachers_listbox()

    def ui_delete_selected_teacher(self):
        sel = self.lb_teachers.curselection()
        if not sel:
            messagebox.showinfo("Delete", "Select a teacher.")
            return
        line = self.lb_teachers.get(sel[0])
        teacher_code = line.split(" — ")[0]
        rows = list_teachers_db()
        row = next((r for r in rows if r['teacher_id'] == teacher_code), None)
        if not row:
            messagebox.showerror("Delete", "Teacher not found in DB.")
            return
        if not messagebox.askyesno("Confirm", f"Delete teacher {teacher_code}? This will remove related timetable entries."):
            return
        delete_teacher_db(row['id'])
        self.refresh_teachers_listbox()
        self.refresh_timetable_listbox()
        self.refresh_att_combo()

    def refresh_teachers_listbox(self):
        self.lb_teachers.delete(0, 'end')
        for r in list_teachers_db():
            self.lb_teachers.insert('end', f"{r['teacher_id']} — {r['name']}")

    def ui_add_course(self):
        code = simpledialog.askstring("Course code", "Course code (eg CS101):", parent=self)
        if not code:
            return
        title = simpledialog.askstring("Title", "Course title (optional):", parent=self) or ""
        add_course_db(code.strip(), title.strip())
        messagebox.showinfo("Add Course", "Course added.")
        self.refresh_courses_listbox()

    def ui_delete_selected_course(self):
        sel = self.lb_courses.curselection()
        if not sel:
            messagebox.showinfo("Delete", "Select a course.")
            return
        line = self.lb_courses.get(sel[0])  # e.g. "1) CS101 — Title"
        cid = int(line.split(")")[0])
        if not messagebox.askyesno("Confirm", f"Delete course id {cid}? This will remove timetables for this course."):
            return
        delete_course_db(cid)
        self.refresh_courses_listbox()
        self.refresh_timetable_listbox()
        self.refresh_att_combo()

    def refresh_courses_listbox(self):
        self.lb_courses.delete(0, 'end')
        for r in list_courses_db():
            self.lb_courses.insert('end', f"{r['id']}) {r['code']} — {r['title']}")

    # ---- Timetable tab ----
    def build_timetable_tab(self):
        f = self.tab_timetable
        f.columnconfigure(0, weight=1)
        f.columnconfigure(1, weight=1)

        left = ttk.LabelFrame(f, text="Timetables")
        left.grid(row=0, column=0, sticky='nsew', padx=6, pady=6)
        right = ttk.LabelFrame(f, text="Teacher — mark attendance by student token")
        right.grid(row=0, column=1, sticky='nsew', padx=6, pady=6)

        self.lb_timetables = tk.Listbox(left, height=20)
        self.lb_timetables.pack(fill='both', expand=True, padx=6, pady=6)
        lbtn = ttk.Frame(left); lbtn.pack(fill='x', padx=6, pady=6)
        ttk.Button(lbtn, text="Create", command=self.ui_create_timetable).pack(side='left')
        ttk.Button(lbtn, text="Delete", command=self.ui_delete_selected_timetable).pack(side='left', padx=6)
        ttk.Button(lbtn, text="Refresh", command=self.refresh_timetable_listbox).pack(side='left', padx=6)

        # Right: teacher mark
        ttk.Label(right, text="Paste student's token here:").pack(anchor='w', padx=6, pady=(8,2))
        self.teacher_token_entry = ttk.Entry(right)
        self.teacher_token_entry.pack(fill='x', padx=6)
        ttk.Button(right, text="Mark Attendance (Teacher)", command=self.ui_teacher_mark).pack(fill='x', padx=6, pady=6)
        ttk.Separator(right).pack(fill='x', padx=6, pady=6)
        ttk.Label(right, text="Last payload preview:").pack(anchor='w', padx=6)
        self.last_payload_text = tk.Text(right, height=8)
        self.last_payload_text.pack(fill='both', padx=6, pady=6)
        self.last_qr_canvas = tk.Canvas(right, width=300, height=300, bg='white')
        self.last_qr_canvas.pack(padx=6, pady=6)

        self.refresh_timetable_listbox()

    def ui_create_timetable(self):
        courses = list_courses_db(); teachers = list_teachers_db()
        if not courses or not teachers:
            messagebox.showinfo("Create", "Add courses and teachers first.")
            return
        course_listing = "\n".join([f"{c['id']}) {c['code']}" for c in courses])
        course_id = simpledialog.askinteger("Course", f"Choose course id:\n{course_listing}", parent=self)
        if course_id is None:
            return
        teacher_listing = "\n".join([f"{t['id']}) {t['teacher_id']}" for t in teachers])
        teacher_id = simpledialog.askinteger("Teacher", f"Choose teacher id:\n{teacher_listing}", parent=self)
        if teacher_id is None:
            return
        use_date = messagebox.askyesno("Date or Weekly", "Create a single-date class? (Yes=date, No=weekly day)")
        if use_date:
            date_s = simpledialog.askstring("Date", "Enter date (YYYY-MM-DD):", parent=self)
            day_of_week = None
            if not date_s:
                messagebox.showerror("Input", "Date required.")
                return
        else:
            date_s = None
            dow = simpledialog.askstring("Day", "Enter day of week (e.g. Monday):", parent=self)
            day_of_week = dow.strip().capitalize() if dow else None
            if not day_of_week:
                messagebox.showerror("Input", "Day of week required.")
                return
        start_time_in = simpledialog.askstring("Start time", "Enter start time (hh:mm AM/PM), e.g. 02:30 PM:", parent=self)
        if not start_time_in:
            messagebox.showerror("Input", "Start time required.")
            return
        try:
            start_24 = parse_time_12h_to_24h(start_time_in)
        except ValueError as e:
            messagebox.showerror("Time", str(e))
            return
        dur = simpledialog.askinteger("Duration", "Duration minutes:", parent=self, initialvalue=60)
        batch = simpledialog.askstring("Batch", "Batch/program (optional):", parent=self) or ""
        section = simpledialog.askstring("Section", "Section (optional):", parent=self) or ""
        create_timetable_db(course_id, teacher_id, date_s, day_of_week, start_24, int(dur or 60), batch.strip(), section.strip())
        messagebox.showinfo("Created", "Timetable entry created.")
        self.refresh_timetable_listbox()
        self.refresh_att_combo()
        self.refresh_student_tt_list()

    def ui_delete_selected_timetable(self):
        sel = self.lb_timetables.curselection()
        if not sel:
            messagebox.showinfo("Delete", "Select a timetable entry.")
            return
        text = self.lb_timetables.get(sel[0])
        tt_id = int(text.split(")")[0])
        if not messagebox.askyesno("Confirm", f"Delete timetable id {tt_id}? This removes tokens & attendance for that entry."):
            return
        delete_timetable_db(tt_id)
        self.refresh_timetable_listbox()
        self.refresh_att_combo()
        self.refresh_student_tt_list()

    def refresh_timetable_listbox(self):
        self.lb_timetables.delete(0, 'end')
        for tt in list_timetables_db():
            when = safe(tt, 'date') if safe(tt, 'date') else safe(tt, 'day_of_week')
            t12 = format_time_24h_to_12h(safe(tt, 'start_time', '00:00'))
            batch = safe(tt, 'batch', '')
            section = safe(tt, 'section', '')
            batchsec = f"{batch}/{section}" if (batch or section) else ""
            tokenflag = "YES" if safe(tt, 'token') else "NO"
            line = f"{tt['id']}) {safe(tt, 'course_code')} {when} {t12} dur {safe(tt, 'duration_minutes', '')}m {batchsec} Token:{tokenflag}"
            self.lb_timetables.insert('end', line)

    def ui_teacher_mark(self):
        token = self.teacher_token_entry.get().strip()
        if not token:
            messagebox.showinfo("Input", "Paste token.")
            return
        ok, msg = teacher_mark_by_token_db(token)
        if ok:
            messagebox.showinfo("Success", msg)
        else:
            messagebox.showerror("Failed", msg)
        self.display_last_payload_from_token(token)
        self.refresh_att_combo()

    def display_last_payload_from_token(self, token):
        conn = get_conn()
        row = conn.execute("""
            SELECT st.*, s.student_id as student_code, c.code as course_code, t.date, t.day_of_week, t.start_time
            FROM student_tokens st
            JOIN student s ON s.id = st.student_id
            JOIN timetable t ON t.id = st.timetable_id
            JOIN course c ON c.id = t.course_id
            WHERE st.token = ?
        """, (token,)).fetchone()
        conn.close()
        if not row:
            return
        payload = {
            "timetable_id": row['timetable_id'],
            "course_code": row['course_code'],
            "student_id": row['student_code'],
            "token": row['token'],
            "expires_at": row['expires_at']
        }
        self.last_payload_text.delete('1.0','end')
        self.last_payload_text.insert('1.0', json.dumps(payload))
        files = [os.path.join(QR_IMAGE_DIR, f) for f in os.listdir(QR_IMAGE_DIR) if f.startswith("student_qr_")]
        if files:
            latest = sorted(files)[-1]
            try:
                img = tk.PhotoImage(file=latest)
                self._qr_photo = img
                self.last_qr_canvas.delete('all')
                self.last_qr_canvas.create_image(150,150, image=img)
            except Exception:
                self.last_qr_canvas.delete('all')
                self.last_qr_canvas.create_text(150,150, text=f"QR saved: {latest}", width=260)

    # ---- Student tab ----
    def build_student_tab(self):
        f = self.tab_student
        top = ttk.LabelFrame(f, text="Student — generate token")
        top.pack(fill='x', padx=6, pady=6)
        ttk.Label(top, text="Student ID:").grid(row=0, column=0, sticky='w', padx=6, pady=6)
        self.e_student_id = ttk.Entry(top)
        self.e_student_id.grid(row=0, column=1, sticky='ew', padx=6)
        ttk.Label(top, text="Choose timetable:").grid(row=1, column=0, sticky='w', padx=6, pady=6)
        self.cb_student_tt = ttk.Combobox(top, state='readonly')
        self.cb_student_tt.grid(row=1, column=1, sticky='ew', padx=6)
        ttk.Button(top, text="Refresh entries", command=self.refresh_student_tt_list).grid(row=0, column=2, padx=6)
        ttk.Button(top, text="Generate Token (Student)", command=self.ui_student_generate_token).grid(row=2, column=0, columnspan=3, pady=6)

        top.columnconfigure(1, weight=1)

        box = ttk.LabelFrame(f, text="Payload / QR")
        box.pack(fill='both', expand=True, padx=6, pady=6)
        self.t_student_payload = tk.Text(box, height=8)
        self.t_student_payload.pack(fill='both', padx=6, pady=6)
        ttk.Button(box, text="Copy token to clipboard", command=self.ui_copy_token).pack(padx=6, pady=4)
        self.canvas_student_qr = tk.Canvas(box, width=300, height=300, bg='white')
        self.canvas_student_qr.pack(padx=6, pady=6)

        self.refresh_student_tt_list()

    def refresh_student_tt_list(self):
        vals = []
        for tt in list_timetables_db():
            when = safe(tt, 'date') if safe(tt, 'date') else safe(tt, 'day_of_week')
            t12 = format_time_24h_to_12h(safe(tt, 'start_time', '00:00'))
            batchsec = f"{safe(tt,'batch','')}/{safe(tt,'section','')}" if (safe(tt,'batch') or safe(tt,'section')) else ""
            label = f"{tt['id']}) {safe(tt, 'course_code')} {when} {t12} {batchsec}".strip()
            vals.append(label)
        self.cb_student_tt['values'] = vals

    def ui_student_generate_token(self):
        sid = self.e_student_id.get().strip()
        sel = self.cb_student_tt.get().strip()
        if not sid or not sel:
            messagebox.showinfo("Input", "Enter student id and select timetable.")
            return
        tt_id = int(sel.split(")")[0])
        ok, payload_or_msg = student_generate_token_db(sid, tt_id)
        if not ok:
            messagebox.showerror("Error", payload_or_msg)
            return
        payload_json = json.dumps(payload_or_msg)
        self.t_student_payload.delete('1.0','end')
        self.t_student_payload.insert('1.0', payload_json)
        fname = os.path.join(QR_IMAGE_DIR, f"student_qr_tt{tt_id}_{int(datetime.now().timestamp())}.png")
        okf, err = fetch_qr_image(payload_json, fname)
        if okf:
            try:
                img = tk.PhotoImage(file=fname)
                self._qr_photo = img
                self.canvas_student_qr.delete('all')
                self.canvas_student_qr.create_image(150,150, image=img)
            except Exception:
                self.canvas_student_qr.delete('all')
                self.canvas_student_qr.create_text(150,150, text=f"QR saved to {fname}", width=260)
        else:
            self.canvas_student_qr.delete('all')
            self.canvas_student_qr.create_text(150,150, text=f"QR fetch failed:\n{err}", width=260)
        self.last_payload_text.delete('1.0','end')
        self.last_payload_text.insert('1.0', payload_json)
        self.refresh_att_combo()

    def ui_copy_token(self):
        txt = self.t_student_payload.get('1.0','end').strip()
        if not txt:
            messagebox.showinfo("Copy", "No payload")
            return
        try:
            data = json.loads(txt)
            token = data.get('token', txt)
        except Exception:
            token = txt
        self.clipboard_clear()
        self.clipboard_append(token)
        messagebox.showinfo("Copied", "Token copied to clipboard")

    # ---- QR tab ----
    def build_qr_tab(self):
        f = self.tab_qr
        ttk.Label(f, text="Load last student token payload").pack(anchor='w', padx=6, pady=6)
        self.t_last_payload = tk.Text(f, height=8)
        self.t_last_payload.pack(fill='both', padx=6, pady=6)
        ttk.Button(f, text="Load last payload", command=self.ui_load_last_payload).pack(padx=6, pady=4)
        self.lbl_last_qr = ttk.Label(f)
        self.lbl_last_qr.pack(padx=6, pady=6)

    def ui_load_last_payload(self):
        conn = get_conn()
        row = conn.execute("""
            SELECT st.*, s.student_id as student_code, c.code as course_code
            FROM student_tokens st
            JOIN student s ON s.id = st.student_id
            JOIN timetable t ON t.id = st.timetable_id
            JOIN course c ON c.id = t.course_id
            ORDER BY st.generated_at DESC LIMIT 1
        """).fetchone()
        conn.close()
        if not row:
            messagebox.showinfo("None", "No student token found")
            return
        payload = {
            "timetable_id": row['timetable_id'],
            "course_code": row['course_code'],
            "student_id": row['student_code'],
            "token": row['token'],
            "expires_at": row['expires_at']
        }
        self.t_last_payload.delete('1.0','end')
        self.t_last_payload.insert('1.0', json.dumps(payload))
        files = [os.path.join(QR_IMAGE_DIR, f) for f in os.listdir(QR_IMAGE_DIR) if f.startswith("student_qr_tt")]
        if files:
            latest = sorted(files)[-1]
            try:
                img = tk.PhotoImage(file=latest)
                self._qr_photo = img
                self.lbl_last_qr.config(image=img, text="")
            except Exception:
                self.lbl_last_qr.config(text=f"QR saved: {latest}")

    # ---- Attendance tab ----
    def build_att_tab(self):
        f = self.tab_att
        top = ttk.Frame(f)
        top.pack(fill='x', padx=6, pady=6)
        ttk.Label(top, text="Select timetable:").pack(side='left', padx=6)
        self.cb_att_tt = ttk.Combobox(top, state='readonly')
        self.cb_att_tt.pack(side='left', padx=6)
        ttk.Button(top, text="Refresh", command=self.refresh_att_combo).pack(side='left', padx=6)
        ttk.Button(top, text="View", command=self.ui_view_attendance).pack(side='left', padx=6)
        ttk.Button(top, text="Export CSV", command=self.ui_export_attendance).pack(side='left', padx=6)

        self.tree_att = ttk.Treeview(f, columns=('student','name','timestamp'), show='headings')
        self.tree_att.heading('student', text='Student ID')
        self.tree_att.heading('name', text='Name')
        self.tree_att.heading('timestamp', text='Timestamp')
        self.tree_att.pack(fill='both', expand=True, padx=6, pady=6)

        self.refresh_att_combo()

    def refresh_att_combo(self):
        vals = []
        for tt in list_timetables_db():
            when = safe(tt, 'date') if safe(tt, 'date') else safe(tt, 'day_of_week')
            t12 = format_time_24h_to_12h(safe(tt, 'start_time', '00:00'))
            vals.append(f"{tt['id']}) {safe(tt, 'course_code')} {when} {t12}")
        self.cb_att_tt['values'] = vals

    def ui_view_attendance(self):
        sel = self.cb_att_tt.get().strip()
        if not sel:
            messagebox.showinfo("Select", "Choose a timetable entry")
            return
        tt_id = int(sel.split(")")[0])
        rows = view_attendance_for_timetable_db(tt_id)
        for i in self.tree_att.get_children(): self.tree_att.delete(i)
        for r in rows:
            self.tree_att.insert('', 'end', values=(r['student_id'], r['name'], r['timestamp']))

    def ui_export_attendance(self):
        sel = self.cb_att_tt.get().strip()
        if not sel:
            messagebox.showinfo("Select", "Choose a timetable entry")
            return
        tt_id = int(sel.split(")")[0])
        fn = filedialog.asksaveasfilename(defaultextension='.csv', filetypes=[('CSV','*.csv')], initialfile=f"attendance_tt{tt_id}.csv")
        if not fn:
            return
        ok, msg = export_attendance_csv_db(tt_id, fn)
        if ok:
            messagebox.showinfo("Export", msg)
        else:
            messagebox.showerror("Export", msg)

    # ---- Global refresh ----
    def refresh_all(self):
        self.refresh_students_listbox()
        self.refresh_teachers_listbox()
        self.refresh_courses_listbox()
        self.refresh_timetable_listbox()
        self.refresh_student_tt_list()
        self.refresh_att_combo()

# ----- Demo data (optional) -----
def init_demo_data():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO teacher (teacher_id, name) VALUES (?, ?)", ("TCH001", "Prof A"))
    cur.execute("INSERT OR IGNORE INTO teacher (teacher_id, name) VALUES (?, ?)", ("TCH002", "Prof B"))
    cur.execute("INSERT OR IGNORE INTO student (student_id, name) VALUES (?, ?)", ("STU001", "Alice"))
    cur.execute("INSERT OR IGNORE INTO student (student_id, name) VALUES (?, ?)", ("STU002", "Bob"))
    cur.execute("INSERT OR IGNORE INTO course (code, title) VALUES (?, ?)", ("CS101", "Intro to CS"))
    conn.commit()
    conn.close()

# ----- Entry point -----
if __name__ == '__main__':
    init_db()
    # create demo data if DB empty
    conn = get_conn()
    try:
        if not conn.execute("SELECT 1 FROM course LIMIT 1").fetchone():
            init_demo_data()
    finally:
        conn.close()
    app = UMSApp()
    app.mainloop()
