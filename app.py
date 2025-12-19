from datetime import datetime, timedelta
from functools import wraps
import os

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    session,
    g,
    send_file
)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy import text
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# ============================================================
#  Flask App & Database Setup
# ============================================================

app = Flask(__name__)

# 🔐 Secret key from environment variable (with fallback)
app.secret_key = os.getenv("SECRET_KEY", "super-secret-key-change-in-production")

# ============================================================
#  Database Configuration - Supabase PostgreSQL
# ============================================================
# Get database URL from environment variable
# Format: postgresql://postgres.[project-ref]:[password]@aws-0-[region].pooler.supabase.com:6543/postgres
DATABASE_URL = os.getenv("DATABASE_URL")

if DATABASE_URL:
    # Use Supabase PostgreSQL
    app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
else:
    # Fallback to SQLite for local development
    BASE_DIR = os.path.abspath(os.path.dirname(__file__))
    db_path = os.path.join(BASE_DIR, "clinic_finance.db")
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{db_path}"
    print("⚠️  WARNING: Using local SQLite database. Set DATABASE_URL for Supabase.")

app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "pool_recycle": 300,
    "pool_pre_ping": True,
}

db = SQLAlchemy(app)


# ============================================================
#  Database Models
# ============================================================

class User(db.Model):
    """Application users (admin or employee)."""
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    # role → "admin" or "employee"
    role = db.Column(db.String(20), nullable=False, default="employee")

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)


class DaySummary(db.Model):
    """Daily collection summary."""
    __tablename__ = "days"

    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, unique=True, nullable=False)

    collect_soft_new = db.Column(db.Float, default=0)
    collect_soft_old = db.Column(db.Float, default=0)
    total_collection = db.Column(db.Float, default=0)

    # Old fields (left for compatibility)
    tvs_qty = db.Column(db.Integer, default=0)
    ult_qty = db.Column(db.Integer, default=0)
    pc_qty = db.Column(db.Integer, default=0)

    cash_in_hand = db.Column(db.Float, default=0)  # optional per-day field
    notes = db.Column(db.Text)

    # when the row was created (exact timestamp)
    created_at = db.Column(db.DateTime, default=datetime.now)
    
    # who created this entry
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_by = db.relationship("User", foreign_keys=[created_by_id])

    def recalc_total(self):
        self.total_collection = (self.collect_soft_new or 0) + (self.collect_soft_old or 0)


class Expense(db.Model):
    """Generic expense rows."""
    __tablename__ = "expenses"

    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False)
    category = db.Column(db.String(100), nullable=False)
    description = db.Column(db.String(255))
    amount = db.Column(db.Float, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now)
    
    # who created this entry
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_by = db.relationship("User", foreign_keys=[created_by_id])


class DoctorBill(db.Model):
    """Doctor payment for TVS / ULTRA / LAB etc. (kept for compatibility)."""
    __tablename__ = "doctor_bills"

    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False)
    doctor_name = db.Column(db.String(100), nullable=False)
    modality = db.Column(db.String(50), nullable=False)  # TVS, ULTRA, LAB, etc.
    amount = db.Column(db.Float, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now)
    
    # who created this entry
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_by = db.relationship("User", foreign_keys=[created_by_id])


class BalanceEntry(db.Model):
    """Bank ledger: deposits, withdrawals and running balance."""
    __tablename__ = "balance_entries"

    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False)
    description = db.Column(db.String(255))
    credit = db.Column(db.Float, default=0)      # money going into bank
    debit = db.Column(db.Float, default=0)       # money going out of bank
    balance_after = db.Column(db.Float, default=0)
    
    # who created this entry
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_by = db.relationship("User", foreign_keys=[created_by_id])


class Staff(db.Model):
    """Staff list with base monthly salary."""
    __tablename__ = "staffs"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    designation = db.Column(db.String(120))
    salary = db.Column(db.Float, default=0)      # base monthly salary
    active = db.Column(db.Boolean, default=True)

    payments = db.relationship("StaffPayment", backref="staff", lazy="dynamic")


class StaffPayment(db.Model):
    """Individual salary payments (partial, from cash or bank)."""
    __tablename__ = "staff_payments"

    id = db.Column(db.Integer, primary_key=True)
    staff_id = db.Column(db.Integer, db.ForeignKey("staffs.id"), nullable=False)
    date = db.Column(db.Date, nullable=False, default=datetime.today)
    amount = db.Column(db.Float, nullable=False)
    source = db.Column(db.String(10), nullable=False)  # "cash" or "bank"
    note = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=datetime.now)

    # links to related Expense and Bank entry (for clean deletion)
    expense_id = db.Column(db.Integer, nullable=True)
    bank_entry_id = db.Column(db.Integer, nullable=True)
    
    # who created this entry
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_by = db.relationship("User", foreign_keys=[created_by_id])


class ExpenseTemplate(db.Model):
    """Reusable expense types (for dropdown in Add Expense)."""
    __tablename__ = "expense_templates"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, unique=True)
    default_description = db.Column(db.String(255))
    default_amount = db.Column(db.Float, default=0)


class DeleteLog(db.Model):
    """Audit log of deletions performed by users."""
    __tablename__ = "delete_logs"

    id = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(db.DateTime, default=datetime.now, nullable=False)

    user_id = db.Column(db.Integer)
    username = db.Column(db.String(80))

    entity_type = db.Column(db.String(50))   # e.g. "expense", "day", "doctor_bill", etc.
    entity_id = db.Column(db.Integer)
    description = db.Column(db.String(255))


class Supplier(db.Model):
    """Suppliers that we pay part by part."""
    __tablename__ = "suppliers"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    details = db.Column(db.String(255))
    total_due = db.Column(db.Float, default=0)  # calculated from bills

    payments = db.relationship("SupplierPayment", backref="supplier", lazy="dynamic")
    bills = db.relationship("SupplierBill", backref="supplier", lazy="dynamic")

    def recalc_total_due(self):
        """Recalculate total_due from all bills."""
        self.total_due = self.bills.with_entities(db.func.sum(SupplierBill.amount)).scalar() or 0


class SupplierBill(db.Model):
    """Bills/invoices from suppliers that add to total payable."""
    __tablename__ = "supplier_bills"

    id = db.Column(db.Integer, primary_key=True)
    supplier_id = db.Column(db.Integer, db.ForeignKey("suppliers.id"), nullable=False)
    date = db.Column(db.Date, nullable=False, default=datetime.today)
    description = db.Column(db.String(255))
    amount = db.Column(db.Float, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now)
    
    # who created this entry
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_by = db.relationship("User", foreign_keys=[created_by_id])


class SupplierPayment(db.Model):
    """Individual payments to suppliers."""
    __tablename__ = "supplier_payments"

    id = db.Column(db.Integer, primary_key=True)
    supplier_id = db.Column(db.Integer, db.ForeignKey("suppliers.id"), nullable=False)
    date = db.Column(db.Date, nullable=False, default=datetime.today)
    amount = db.Column(db.Float, nullable=False)
    source = db.Column(db.String(10), nullable=False)  # "cash" or "bank"
    note = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=datetime.now)

    expense_id = db.Column(db.Integer, nullable=True)
    bank_entry_id = db.Column(db.Integer, nullable=True)
    
    # who created this entry
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_by = db.relationship("User", foreign_keys=[created_by_id])


class LabCollection(db.Model):
    """Daily collection amount coming from Lab (one per day)."""
    __tablename__ = "lab_collections"

    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False)
    amount = db.Column(db.Float, nullable=False)
    note = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=datetime.now)
    
    # who created this entry
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_by = db.relationship("User", foreign_keys=[created_by_id])


class StaffMonthlySalary(db.Model):
    """
    Salary change record for a staff member.
    The salary is effective from the specified year/month onwards until a newer record exists.
    """
    __tablename__ = "staff_monthly_salaries"

    id = db.Column(db.Integer, primary_key=True)
    staff_id = db.Column(db.Integer, db.ForeignKey("staffs.id"), nullable=False)
    year = db.Column(db.Integer, nullable=False)       # Effective from this year
    month = db.Column(db.Integer, nullable=False)      # Effective from this month (1-12)
    salary = db.Column(db.Float, nullable=False)
    note = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=datetime.now)

    # Unique constraint: one salary change per staff per month
    __table_args__ = (
        db.UniqueConstraint('staff_id', 'year', 'month', name='unique_staff_month_salary'),
    )

    staff = db.relationship("Staff", backref=db.backref("monthly_salaries", lazy="dynamic"))


# ============================================================
#  Auth Helpers
# ============================================================

def login_required(view):
    @wraps(view)
    def wrapped_view(**kwargs):
        if g.user is None:
            return redirect(url_for("login", next=request.path))
        return view(**kwargs)
    return wrapped_view

def admin_required(view):
    @wraps(view)
    def wrapped_view(**kwargs):
        if g.user is None:
            return redirect(url_for("login", next=request.path))
        if getattr(g.user, "role", "employee") not in ("admin","superadmin"):
            flash("You do not have permission to access this page.")
            return redirect(url_for("dashboard"))
        return view(**kwargs)
    return wrapped_view

def superadmin_required(view):
    @wraps(view)
    def wrapped_view(**kwargs):
        if g.user is None:
            return redirect(url_for("login", next=request.path))
        if getattr(g.user, "role", "employee") != "superadmin":
            flash("You do not have permission to access this page.")
            return redirect(url_for("dashboard"))
        return view(**kwargs)
    return wrapped_view

@app.before_request
def load_logged_in_user():
    user_id = session.get("user_id")
    g.user = User.query.get(user_id) if user_id else None


# ============================================================
#  Manage Users (superadmin only)
# ============================================================

@app.route("/admin/users", methods=["GET","POST"])
@superadmin_required
def manage_users():
    if request.method == "POST":
        form_type = request.form.get("form_type")
        if form_type=="add_user":
            username = (request.form.get("username") or "").strip()
            password = request.form.get("password") or ""
            role = request.form.get("role") or "employee"
            if not username or not password:
                flash("Username and password required"); return redirect(url_for("manage_users"))
            if role not in ("admin","employee"): role="employee"
            if User.query.filter_by(username=username).first():
                flash("Username already exists"); return redirect(url_for("manage_users"))
            new_user = User(username=username, role=role)
            new_user.set_password(password)
            db.session.add(new_user); db.session.commit()
            flash(f"User '{username}' ({role}) created."); return redirect(url_for("manage_users"))
    users = User.query.order_by(User.username.asc()).all()
    return render_template("manage_users.html", users=users)

@app.route("/admin/users/<int:user_id>/delete", methods=["POST"])
@superadmin_required
def delete_user(user_id):
    user = User.query.get_or_404(user_id)
    if user.id==g.user.id or user.username=="dp_mamun":
        flash("Cannot delete this account."); return redirect(url_for("manage_users"))
    db.session.delete(user); db.session.commit()
    flash(f"User '{user.username}' deleted."); return redirect(url_for("manage_users"))

# ============================================================
#  Change password (all users)
# ============================================================

@app.route("/change-password", methods=["GET","POST"])
@login_required
def change_password():
    if request.method=="POST":
        old_pass=request.form.get("old_password") or ""
        new_pass=request.form.get("new_password") or ""
        confirm_pass=request.form.get("confirm_password") or ""
        if not old_pass or not new_pass or not confirm_pass:
            flash("All fields are required"); return redirect(url_for("change_password"))
        if not g.user.check_password(old_pass):
            flash("Old password incorrect"); return redirect(url_for("change_password"))
        if new_pass!=confirm_pass:
            flash("New password mismatch"); return redirect(url_for("change_password"))
        g.user.set_password(new_pass); db.session.commit()
        flash("Password updated"); return redirect(url_for("dashboard"))
    return render_template("change_password.html")


# ============================================================
#  Utility Functions
# ============================================================

def recalc_bank_balances():
    """Recompute balance_after for all bank entries in chronological order."""
    entries = BalanceEntry.query.order_by(BalanceEntry.date, BalanceEntry.id).all()
    running = 0.0
    for e in entries:
        running += (e.credit or 0) - (e.debit or 0)
        e.balance_after = running
    db.session.commit()


def log_delete(entity_type: str, entity_id: int, description: str):
    """Create a delete-log entry (no commit here)."""
    user_id = getattr(g.user, "id", None)
    username = getattr(g.user, "username", None)

    entry = DeleteLog(
        user_id=user_id,
        username=username,
        entity_type=entity_type,
        entity_id=entity_id,
        description=description[:255] if description else None,
    )
    db.session.add(entry)


def get_staff_salary_for_month(staff, year: int, month: int) -> tuple:
    """
    Get the effective salary for a staff member for a specific month.
    Returns (salary, salary_record_or_None).
    
    Finds the most recent salary change that is effective for the given month
    (year/month <= requested year/month). If no change exists, returns base salary.
    """
    # Find the most recent salary change effective on or before the requested month
    # Order by year DESC, month DESC to get the most recent one first
    salary_record = StaffMonthlySalary.query.filter(
        StaffMonthlySalary.staff_id == staff.id,
        db.or_(
            StaffMonthlySalary.year < year,
            db.and_(
                StaffMonthlySalary.year == year,
                StaffMonthlySalary.month <= month
            )
        )
    ).order_by(
        StaffMonthlySalary.year.desc(),
        StaffMonthlySalary.month.desc()
    ).first()
    
    if salary_record:
        return salary_record.salary, salary_record
    return staff.salary or 0, None


# ============================================================
#  Dashboard & Core Finance Routes
# ============================================================

@app.route("/")
@login_required
def dashboard():
    """
    Main dashboard with date-range filters:
      - Recent Days (collections) → default = current month
      - Expenses table           → default = current day
      - Total expenses card      → current month only
      - Collections include LabCollection.
    """

    today = datetime.today().date()
    first_of_month = today.replace(day=1)

    # --------------------------------------------------
    # 1) Date-range for "Recent Days" (collections)
    # --------------------------------------------------
    days_start_str = request.args.get("days_start")
    days_end_str = request.args.get("days_end")

    if days_start_str and days_end_str:
        try:
            days_start = datetime.strptime(days_start_str, "%Y-%m-%d").date()
            days_end = datetime.strptime(days_end_str, "%Y-%m-%d").date()
        except ValueError:
            days_start = first_of_month
            days_end = today
    else:
        days_start = first_of_month
        days_end = today

    days = DaySummary.query.filter(
        DaySummary.date >= days_start,
        DaySummary.date <= days_end,
    ).order_by(DaySummary.date.desc()).all()

    # Lab collections in same range
    lab_rows_range = LabCollection.query.filter(
        LabCollection.date >= days_start,
        LabCollection.date <= days_end,
    ).all()
    lab_by_date = {r.date: r for r in lab_rows_range}
    total_lab_range = sum(r.amount or 0 for r in lab_rows_range)

    # total collection (normal + lab) for the range
    total_collection_range = (
        sum(d.total_collection or 0 for d in days) + total_lab_range
    )

    # --------------------------------------------------
    # 2) Total expenses card: current month only
    # --------------------------------------------------
    total_expenses_month = db.session.query(
        db.func.sum(Expense.amount)
    ).filter(
        Expense.date >= first_of_month,
        Expense.date <= today,
    ).scalar() or 0

    # --------------------------------------------------
    # 3) Expenses table date-range (default = today)
    # --------------------------------------------------
    exp_start_str = request.args.get("exp_start")
    exp_end_str = request.args.get("exp_end")

    if exp_start_str and exp_end_str:
        try:
            exp_start = datetime.strptime(exp_start_str, "%Y-%m-%d").date()
            exp_end = datetime.strptime(exp_end_str, "%Y-%m-%d").date()
        except ValueError:
            exp_start = today
            exp_end = today
    else:
        exp_start = today
        exp_end = today

    expenses = Expense.query.filter(
        Expense.date >= exp_start,
        Expense.date <= exp_end,
    ).order_by(Expense.date.desc(), Expense.id.desc()).all()

    # Doctor bills – still loaded, even if not used much
    total_doctor_bills_all = db.session.query(
        db.func.sum(DoctorBill.amount)
    ).scalar() or 0
    doctor_bills = DoctorBill.query.order_by(
        DoctorBill.date.desc(), DoctorBill.id.desc()
    ).all()

    # --------------------------------------------------
    # 4) Cash in hand & bank from all-time data
    # --------------------------------------------------
    total_collection_all_normal = db.session.query(
        db.func.sum(DaySummary.total_collection)
    ).scalar() or 0

    total_lab_all = db.session.query(
        db.func.sum(LabCollection.amount)
    ).scalar() or 0

    total_collection_all = total_collection_all_normal + total_lab_all

    total_expenses_all = db.session.query(
        db.func.sum(Expense.amount)
    ).scalar() or 0

    total_wealth = total_collection_all - total_expenses_all - total_doctor_bills_all

    bank_credits = db.session.query(
        db.func.sum(BalanceEntry.credit)
    ).scalar() or 0
    bank_debits = db.session.query(
        db.func.sum(BalanceEntry.debit)
    ).scalar() or 0
    bank_balance = bank_credits - bank_debits

    cash_in_hand = total_wealth - bank_balance

    return render_template(
        "dashboard.html",
        # Days range + total
        days=days,
        days_start=days_start,
        days_end=days_end,
        total_collection_range=total_collection_range,

        # Cards
        total_expenses_month=total_expenses_month,
        total_doctor_bills_all=total_doctor_bills_all,
        cash_in_hand=cash_in_hand,
        bank_balance=bank_balance,

        # Expenses range
        expenses=expenses,
        exp_start=exp_start,
        exp_end=exp_end,

        # Doctor bills table
        doctor_bills=doctor_bills,

        # Lab collections by date for table display
        lab_by_date=lab_by_date,
    )


@app.route("/day/add", methods=["GET", "POST"])
@login_required
def add_day():
    """Create or update a day summary."""
    if request.method == "POST":
        date_str = request.form.get("date")
        collect_soft_new = float(request.form.get("collect_soft_new") or 0)
        collect_soft_old = float(request.form.get("collect_soft_old") or 0)

        # old fields (ignored in UI but kept for compatibility)
        tvs_qty = int(request.form.get("tvs_qty") or 0)
        ult_qty = int(request.form.get("ult_qty") or 0)
        pc_qty = int(request.form.get("pc_qty") or 0)
        cash_in_hand = float(request.form.get("cash_in_hand") or 0)

        notes = request.form.get("notes") or ""

        try:
            d = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            flash("Invalid date format. Use YYYY-MM-DD.")
            return redirect(url_for("add_day"))

        existing = DaySummary.query.filter_by(date=d).first()

        if existing and (g.user.role != "admin"):
            flash("Only admin can modify an existing day summary for this date.")
            return redirect(url_for("dashboard"))

        day = existing or DaySummary(date=d, created_by_id=g.user.id)
        day.collect_soft_new = collect_soft_new
        day.collect_soft_old = collect_soft_old
        day.tvs_qty = tvs_qty
        day.ult_qty = ult_qty
        day.pc_qty = pc_qty
        day.cash_in_hand = cash_in_hand
        day.notes = notes
        day.recalc_total()
        
        # Update created_by if it was modified by someone else
        if existing:
            day.created_by_id = g.user.id

        db.session.add(day)
        db.session.commit()
        flash("Day summary saved.")
        return redirect(url_for("dashboard"))

    return render_template("add_day.html")


# -------------------- Lab Collection --------------------

@app.route("/lab-collection", methods=["GET", "POST"])
@login_required
def lab_collection():
    """
    Collection from Lab:
    - one entry per day (overwrites if same date is submitted again)
    - always ensures a DaySummary row exists for that date
    - admin can delete rows via delete_lab_collection.
    """
    if request.method == "POST":
        date_str = request.form.get("date")
        amount_str = request.form.get("amount") or "0"
        note = (request.form.get("note") or "").strip()

        # date: empty = today
        if date_str:
            try:
                d = datetime.strptime(date_str, "%Y-%m-%d").date()
            except ValueError:
                flash("Invalid date format. Use YYYY-MM-DD.")
                return redirect(url_for("lab_collection"))
        else:
            d = datetime.today().date()

        # amount
        try:
            amount = float(amount_str)
        except ValueError:
            flash("Invalid amount.")
            return redirect(url_for("lab_collection"))

        if amount <= 0:
            flash("Amount must be positive.")
            return redirect(url_for("lab_collection"))

        # ----- ensure DaySummary exists for this date -----
        day = DaySummary.query.filter_by(date=d).first()
        if not day:
            day = DaySummary(
                date=d,
                collect_soft_new=0,
                collect_soft_old=0,
                tvs_qty=0,
                ult_qty=0,
                pc_qty=0,
                cash_in_hand=0,
                notes="",
                created_at=datetime.now(),
                created_by_id=g.user.id,
            )
            db.session.add(day)

        # ----- LabCollection: one row per date (update or create) -----
        lab_row = LabCollection.query.filter_by(date=d).first()
        if not lab_row:
            lab_row = LabCollection(date=d, created_at=datetime.now(), created_by_id=g.user.id)

        lab_row.amount = amount
        lab_row.note = note
        lab_row.created_by_id = g.user.id  # Update who last modified
        db.session.add(lab_row)

        db.session.commit()

        flash("Lab collection saved for this date.")
        return redirect(url_for("lab_collection"))

    # GET: show recent lab rows
    recent = LabCollection.query.order_by(
        LabCollection.date.desc(), LabCollection.id.desc()
    ).limit(30).all()

    return render_template("lab_collection.html", recent=recent)



@app.route("/lab-collection/<int:lab_id>/delete", methods=["POST"])
@admin_required
def delete_lab_collection(lab_id):
    row = LabCollection.query.get_or_404(lab_id)
    db.session.delete(row)
    db.session.commit()
    flash("Lab collection entry deleted.")
    return redirect(url_for("lab_collection"))


# -------------------- Expense (with templates) --------------------

@app.route("/expense/add", methods=["GET", "POST"])
@login_required
def add_expense():
    """Add a new expense (admin + staff)."""

    if request.method == "POST":
        date_str = request.form.get("date")  # can be empty
        template_id = request.form.get("template_id")  # from dropdown
        category = (request.form.get("category") or "").strip()
        description = (request.form.get("description") or "").strip()
        amount_str = request.form.get("amount") or ""

        # If a template is selected, use its values (where fields are empty)
        tmpl = None
        if template_id:
            try:
                tmpl_id_int = int(template_id)
                tmpl = ExpenseTemplate.query.get(tmpl_id_int)
            except (TypeError, ValueError):
                tmpl = None

        if tmpl:
            if not category:
                category = tmpl.name
            if not description:
                description = tmpl.default_description or ""
            if not amount_str:
                amount_str = str(tmpl.default_amount or 0)

        if not category:
            category = "General"

        try:
            amount = float(amount_str or 0)
        except ValueError:
            flash("Invalid amount.")
            return redirect(url_for("add_expense"))

        # date: empty = today
        if date_str:
            try:
                d = datetime.strptime(date_str, "%Y-%m-%d").date()
            except ValueError:
                flash("Invalid date format. Use YYYY-MM-DD.")
                return redirect(url_for("add_expense"))
        else:
            d = datetime.today().date()

        exp = Expense(date=d, category=category, description=description, amount=amount, created_by_id=g.user.id)
        db.session.add(exp)
        db.session.commit()
        flash("Expense added.")
        return redirect(url_for("add_expense"))

    templates = ExpenseTemplate.query.order_by(ExpenseTemplate.name).all()
    return render_template("add_expense.html", templates=templates)


@app.route("/doctor-bill/add", methods=["GET", "POST"])
@login_required
def add_doctor_bill():
    """Add a doctor bill (admin + staff)."""
    if request.method == "POST":
        date_str = request.form.get("date")
        doctor_name = request.form.get("doctor_name")
        modality = request.form.get("modality")
        amount = float(request.form.get("amount") or 0)

        try:
            d = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            flash("Invalid date format. Use YYYY-MM-DD.")
            return redirect(url_for("add_doctor_bill"))

        bill = DoctorBill(
            date=d,
            doctor_name=doctor_name,
            modality=modality,
            amount=amount,
            created_by_id=g.user.id,
        )
        db.session.add(bill)
        db.session.commit()
        flash("Doctor bill added.")
        return redirect(url_for("dashboard"))

    return render_template("add_doctor_bill.html")


# ============================================================
#  Delete Routes (Admin Only)
# ============================================================

@app.route("/day/<int:day_id>/delete", methods=["POST"])
@admin_required
def delete_day(day_id):
    day = DaySummary.query.get_or_404(day_id)

    log_delete("day", day.id, f"Deleted day summary for {day.date}")

    db.session.delete(day)
    db.session.commit()
    flash("Day summary deleted.")
    return redirect(url_for("dashboard"))


@app.route("/expense/<int:expense_id>/delete", methods=["POST"])
@admin_required
def delete_expense(expense_id):
    """
    Delete an expense. If it is linked to staff/supplier payments and/or a bank entry,
    those are also removed and bank balances recalculated.
    """
    exp = Expense.query.get_or_404(expense_id)

    log_delete(
        "expense",
        exp.id,
        f"Deleted expense '{exp.category}' {exp.amount:.2f} BDT on {exp.date}",
    )

    touched_bank = False

    # Staff payments linked to this expense
    staff_payments = StaffPayment.query.filter_by(expense_id=exp.id).all()
    for p in staff_payments:
        if p.bank_entry_id:
            be = BalanceEntry.query.get(p.bank_entry_id)
            if be:
                db.session.delete(be)
                touched_bank = True
        db.session.delete(p)

    # Supplier payments linked to this expense
    supplier_payments = SupplierPayment.query.filter_by(expense_id=exp.id).all()
    for sp in supplier_payments:
        if sp.bank_entry_id:
            be = BalanceEntry.query.get(sp.bank_entry_id)
            if be:
                db.session.delete(be)
                touched_bank = True
        db.session.delete(sp)

    db.session.delete(exp)
    db.session.commit()

    if touched_bank:
        recalc_bank_balances()

    flash("Expense deleted.")
    return redirect(url_for("dashboard"))


@app.route("/doctor-bill/<int:bill_id>/delete", methods=["POST"])
@admin_required
def delete_doctor_bill(bill_id):
    bill = DoctorBill.query.get_or_404(bill_id)

    log_delete(
        "doctor_bill",
        bill.id,
        f"Deleted doctor bill for {bill.doctor_name} ({bill.modality}) {bill.amount:.2f} BDT",
    )

    db.session.delete(bill)
    db.session.commit()
    flash("Doctor bill deleted.")
    return redirect(url_for("dashboard"))


@app.route("/admin/empty-db", methods=["POST"])
@admin_required
def empty_database():
    """
    Permanently delete all financial data.
    Does NOT delete any users.
    """
    try:
        log_delete("all_data", 0, "Emptied all financial records from database")

        DaySummary.query.delete()
        Expense.query.delete()
        DoctorBill.query.delete()
        BalanceEntry.query.delete()
        StaffPayment.query.delete()
        Staff.query.delete()
        SupplierBill.query.delete()
        SupplierPayment.query.delete()
        Supplier.query.delete()
        ExpenseTemplate.query.delete()
        DeleteLog.query.delete()
        LabCollection.query.delete()

        db.session.commit()
        flash("All financial records have been permanently deleted.", "warning")
    except Exception as e:
        db.session.rollback()
        flash(f"Error while deleting data: {e}", "danger")

    return redirect(url_for("dashboard"))


# ============================================================
#  Reports – HTML version, opens in new tab
# ============================================================

@app.route("/report", methods=["GET", "POST"])
@login_required
def report():
    """
    Finance report over a date range.
    Supports:
      - report_type: all / collection / expenses / doctor_bills
      - expense_category: optional single category filter
      - LabCollection is included in totals and shown per-day.
    """
    if request.method == "POST":
        start_str = request.form.get("start_date")
        end_str = request.form.get("end_date")

        # --- Parse dates ---
        try:
            start_date = datetime.strptime(start_str, "%Y-%m-%d").date()
            end_date = datetime.strptime(end_str, "%Y-%m-%d").date()
        except (TypeError, ValueError):
            flash("Invalid dates. Please select both start and end date.")
            return redirect(url_for("report"))

        if end_date < start_date:
            flash("End date cannot be earlier than start date.")
            return redirect(url_for("report"))

        # --- What to include? ---
        report_type = request.form.get("report_type", "all")
        include_collection = report_type in ("all", "collection")
        include_expenses = report_type in ("all", "expenses")
        include_doctor_bills = report_type in ("all", "doctor_bills")

        expense_category = (request.form.get("expense_category") or "").strip()

        # --------------------------------------------------
        # COLLECTION (DaySummary + LabCollection)
        # --------------------------------------------------
        if include_collection:
            # Day-wise normal collection
            days = DaySummary.query.filter(
                DaySummary.date >= start_date,
                DaySummary.date <= end_date
            ).order_by(DaySummary.date).all()

            total_normal_collection = sum(d.total_collection or 0 for d in days)

            # Lab collections in the same range
            lab_rows = LabCollection.query.filter(
                LabCollection.date >= start_date,
                LabCollection.date <= end_date
            ).all()
            total_lab = sum(r.amount or 0 for r in lab_rows)

            # Map date -> lab row for template
            lab_by_date = {r.date: r for r in lab_rows}

            # Overall total collection (normal + lab)
            total_collection = total_normal_collection + total_lab
        else:
            days = []
            total_collection = 0
            total_lab = 0
            lab_by_date = {}

        # --------------------------------------------------
        # EXPENSES
        # --------------------------------------------------
        if include_expenses:
            expenses_query = Expense.query.filter(
                Expense.date >= start_date,
                Expense.date <= end_date
            )
            if expense_category:
                expenses_query = expenses_query.filter(
                    Expense.category == expense_category
                )
            expenses_list = expenses_query.order_by(Expense.date).all()

            total_expenses_query = db.session.query(
                db.func.sum(Expense.amount)
            ).filter(
                Expense.date >= start_date,
                Expense.date <= end_date
            )
            if expense_category:
                total_expenses_query = total_expenses_query.filter(
                    Expense.category == expense_category
                )
            total_expenses = total_expenses_query.scalar() or 0

            expense_breakdown_query = db.session.query(
                Expense.category,
                db.func.sum(Expense.amount).label("total")
            ).filter(
                Expense.date >= start_date,
                Expense.date <= end_date
            )
            if expense_category:
                expense_breakdown_query = expense_breakdown_query.filter(
                    Expense.category == expense_category
                )
            expense_breakdown = expense_breakdown_query.group_by(
                Expense.category
            ).order_by(
                Expense.category
            ).all()
        else:
            expenses_list = []
            total_expenses = 0
            expense_breakdown = []

        # --------------------------------------------------
        # DOCTOR BILLS
        # --------------------------------------------------
        if include_doctor_bills:
            doctor_bills_list = DoctorBill.query.filter(
                DoctorBill.date >= start_date,
                DoctorBill.date <= end_date
            ).order_by(DoctorBill.date).all()

            total_doctor_bills = db.session.query(
                db.func.sum(DoctorBill.amount)
            ).filter(
                DoctorBill.date >= start_date,
                DoctorBill.date <= end_date
            ).scalar() or 0

            doctor_breakdown = db.session.query(
                DoctorBill.doctor_name,
                DoctorBill.modality,
                db.func.sum(DoctorBill.amount).label("total")
            ).filter(
                DoctorBill.date >= start_date,
                DoctorBill.date <= end_date
            ).group_by(
                DoctorBill.doctor_name,
                DoctorBill.modality
            ).order_by(
                DoctorBill.doctor_name,
                DoctorBill.modality
            ).all()
        else:
            doctor_bills_list = []
            total_doctor_bills = 0
            doctor_breakdown = []

        # --------------------------------------------------
        # NET CASH (only when everything is included & no category filter)
        # --------------------------------------------------
        if (
            include_collection
            and include_expenses
            and include_doctor_bills
            and not expense_category
        ):
            net_cash = (
                (total_collection or 0)
                - (total_expenses or 0)
                - (total_doctor_bills or 0)
            )
        else:
            net_cash = None

        # --------------------------------------------------
        # Render printable HTML report
        # --------------------------------------------------
        return render_template(
            "finance_report_pdf.html",
            start_date=start_date,
            end_date=end_date,
            timestamp=datetime.now(),

            report_type=report_type,
            include_collection=include_collection,
            include_expenses=include_expenses,
            include_doctor_bills=include_doctor_bills,

            expense_category=expense_category,

            days=days,
            expenses_list=expenses_list,
            doctor_bills_list=doctor_bills_list,

            total_collection=total_collection,
            total_expenses=total_expenses,
            total_doctor_bills=total_doctor_bills,
            net_cash=net_cash,

            expense_breakdown=expense_breakdown,
            doctor_breakdown=doctor_breakdown,

            # Lab-related data
            total_lab=total_lab,
            lab_by_date=lab_by_date,
        )

    # ----------------- GET: show form -----------------
    expense_categories = (
        db.session.query(Expense.category)
        .filter(Expense.category.isnot(None))
        .distinct()
        .order_by(Expense.category)
        .all()
    )
    category_list = [c[0] for c in expense_categories]

    return render_template("report.html", expense_categories=category_list)



# ============================================================
#  Bank Balance & Statements
# ============================================================

@app.route("/bank", methods=["GET", "POST"])
@login_required
def bank():
    """
    Bank balance page:
      - add deposit / withdraw transactions
      - show history
    """
    if request.method == "POST":
        tx_type = request.form.get("tx_type")  # "deposit" or "withdraw"
        amount_str = request.form.get("amount") or "0"
        description = request.form.get("description") or ""
        date_str = request.form.get("date")

        try:
            amount = float(amount_str)
        except ValueError:
            flash("Invalid amount.")
            return redirect(url_for("bank"))

        if amount <= 0:
            flash("Amount must be positive.")
            return redirect(url_for("bank"))

        if date_str:
            try:
                tx_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            except ValueError:
                flash("Invalid date format.")
                return redirect(url_for("bank"))
        else:
            tx_date = datetime.today().date()

        bank_credits = db.session.query(
            db.func.sum(BalanceEntry.credit)
        ).scalar() or 0
        bank_debits = db.session.query(
            db.func.sum(BalanceEntry.debit)
        ).scalar() or 0
        current_balance = bank_credits - bank_debits

        if tx_type == "deposit":
            credit = amount
            debit = 0
            desc = description or "Deposit from cash"
        elif tx_type == "withdraw":
            if amount > current_balance:
                flash("Cannot withdraw more than current bank balance.")
                return redirect(url_for("bank"))
            credit = 0
            debit = amount
            desc = description or "Withdraw to cash"
        else:
            flash("Invalid transaction type.")
            return redirect(url_for("bank"))

        entry = BalanceEntry(
            date=tx_date,
            description=desc,
            credit=credit,
            debit=debit,
            created_by_id=g.user.id,
        )
        db.session.add(entry)
        db.session.commit()

        recalc_bank_balances()
        flash("Bank transaction recorded.")
        return redirect(url_for("bank"))

    bank_credits = db.session.query(
        db.func.sum(BalanceEntry.credit)
    ).scalar() or 0
    bank_debits = db.session.query(
        db.func.sum(BalanceEntry.debit)
    ).scalar() or 0
    bank_balance = bank_credits - bank_debits

    total_collection_all_normal = db.session.query(
        db.func.sum(DaySummary.total_collection)
    ).scalar() or 0
    total_lab_all = db.session.query(
        db.func.sum(LabCollection.amount)
    ).scalar() or 0
    total_collection_all = total_collection_all_normal + total_lab_all

    total_expenses_all = db.session.query(
        db.func.sum(Expense.amount)
    ).scalar() or 0
    total_doctor_bills_all = db.session.query(
        db.func.sum(DoctorBill.amount)
    ).scalar() or 0
    total_wealth = total_collection_all - total_expenses_all - total_doctor_bills_all
    cash_in_hand = total_wealth - bank_balance

    entries = BalanceEntry.query.order_by(
        BalanceEntry.date.desc(), BalanceEntry.id.desc()
    ).all()

    return render_template(
        "bank.html",
        bank_balance=bank_balance,
        cash_in_hand=cash_in_hand,
        entries=entries,
    )


@app.route("/bank/entry/<int:entry_id>/delete", methods=["POST"])
@admin_required
def delete_bank_entry(entry_id):
    entry = BalanceEntry.query.get_or_404(entry_id)

    log_delete(
        "bank_entry",
        entry.id,
        f"Deleted bank entry '{entry.description}' credit={entry.credit} debit={entry.debit}",
    )

    db.session.delete(entry)
    db.session.commit()
    recalc_bank_balances()
    flash("Bank transaction deleted.")
    return redirect(url_for("bank"))


@app.route("/bank/statement", methods=["POST"])
@login_required
def bank_statement():
    """Printable bank statement for a date range."""
    start_str = request.form.get("start_date")
    end_str = request.form.get("end_date")

    try:
        start_date = datetime.strptime(start_str, "%Y-%m-%d").date()
        end_date = datetime.strptime(end_str, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        flash("Invalid dates for bank statement.")
        return redirect(url_for("bank"))

    if end_date < start_date:
        flash("End date cannot be earlier than start date.")
        return redirect(url_for("bank"))

    opening_credits = db.session.query(
        db.func.sum(BalanceEntry.credit)
    ).filter(
        BalanceEntry.date < start_date
    ).scalar() or 0

    opening_debits = db.session.query(
        db.func.sum(BalanceEntry.debit)
    ).filter(
        BalanceEntry.date < start_date
    ).scalar() or 0

    opening_balance = opening_credits - opening_debits

    entries = BalanceEntry.query.filter(
        BalanceEntry.date >= start_date,
        BalanceEntry.date <= end_date
    ).order_by(
        BalanceEntry.date, BalanceEntry.id
    ).all()

    period_credits = db.session.query(
        db.func.sum(BalanceEntry.credit)
    ).filter(
        BalanceEntry.date >= start_date,
        BalanceEntry.date <= end_date
    ).scalar() or 0

    period_debits = db.session.query(
        db.func.sum(BalanceEntry.debit)
    ).filter(
        BalanceEntry.date >= start_date,
        BalanceEntry.date <= end_date
    ).scalar() or 0

    closing_balance = opening_balance + period_credits - period_debits

    return render_template(
        "bank_statement.html",
        start_date=start_date,
        end_date=end_date,
        timestamp=datetime.now(),
        opening_balance=opening_balance,
        closing_balance=closing_balance,
        period_credits=period_credits,
        period_debits=period_debits,
        entries=entries,
    )


# ============================================================
#  Staff Salary Management
# ============================================================

@app.route("/staffs", methods=["GET", "POST"])
@login_required
def staffs():
    """
    Staff salary page:
      - add staff with base salary
      - pay salary step by step (cash or bank)
      - filter by month (default = current month)
    """
    form_type = request.form.get("form_type") if request.method == "POST" else None
    today = datetime.today().date()

    # ---------- Month filter (default = current month) ----------
    filter_month_str = request.args.get("month")  # YYYY-MM format
    if filter_month_str:
        try:
            filter_start = datetime.strptime(filter_month_str + "-01", "%Y-%m-%d").date()
            year = filter_start.year
            month = filter_start.month
            if month == 12:
                filter_end = datetime(year + 1, 1, 1).date() - timedelta(days=1)
            else:
                filter_end = datetime(year, month + 1, 1).date() - timedelta(days=1)
        except ValueError:
            # Invalid format, use current month
            filter_start = today.replace(day=1)
            year = today.year
            month = today.month
            if month == 12:
                filter_end = datetime(year + 1, 1, 1).date() - timedelta(days=1)
            else:
                filter_end = datetime(year, month + 1, 1).date() - timedelta(days=1)
    else:
        # Default to current month
        filter_start = today.replace(day=1)
        year = today.year
        month = today.month
        if month == 12:
            filter_end = datetime(year + 1, 1, 1).date() - timedelta(days=1)
        else:
            filter_end = datetime(year, month + 1, 1).date() - timedelta(days=1)
        filter_month_str = today.strftime("%Y-%m")

    # ---------- Add new staff (admin only) ----------
    if request.method == "POST" and form_type == "add_staff":
        if g.user.role != "admin":
            flash("Only admin can add staff.")
            return redirect(url_for("staffs"))

        name = (request.form.get("name") or "").strip()
        designation = (request.form.get("designation") or "").strip()
        salary_str = request.form.get("salary") or "0"

        if not name:
            flash("Staff name is required.")
            return redirect(url_for("staffs"))

        try:
            salary = float(salary_str)
        except ValueError:
            flash("Invalid salary amount.")
            return redirect(url_for("staffs"))

        staff = Staff(name=name, designation=designation, salary=salary)
        db.session.add(staff)
        db.session.commit()
        flash("Staff member added.")
        return redirect(url_for("staffs"))

    # ---------- Pay salary (partial) ----------
    if request.method == "POST" and form_type == "pay_staff":
        staff_id = request.form.get("staff_id")
        source = request.form.get("source")  # "cash" or "bank"
        amount_str = request.form.get("amount") or "0"
        note = request.form.get("note") or ""
        date_str = request.form.get("date")

        try:
            staff_id = int(staff_id)
        except (TypeError, ValueError):
            flash("Invalid staff selection.")
            return redirect(url_for("staffs"))

        staff = Staff.query.get_or_404(staff_id)

        try:
            amount = float(amount_str)
        except ValueError:
            flash("Invalid amount.")
            return redirect(url_for("staffs"))

        if amount <= 0:
            flash("Amount must be positive.")
            return redirect(url_for("staffs"))

        if date_str:
            try:
                pay_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            except ValueError:
                flash("Invalid date format.")
                return redirect(url_for("staffs"))
        else:
            pay_date = today

        # Get effective salary for the payment month
        pay_year = pay_date.year
        pay_month = pay_date.month
        effective_salary, _ = get_staff_salary_for_month(staff, pay_year, pay_month)
        
        # Calculate month boundaries for payment date
        if pay_month == 12:
            month_end = datetime(pay_year + 1, 1, 1).date() - timedelta(days=1)
        else:
            month_end = datetime(pay_year, pay_month + 1, 1).date() - timedelta(days=1)
        month_start = pay_date.replace(day=1)
        
        # Total paid in the payment month
        total_paid_this_month = staff.payments.filter(
            StaffPayment.date >= month_start,
            StaffPayment.date <= month_end
        ).with_entities(
            db.func.sum(StaffPayment.amount)
        ).scalar() or 0
        
        remaining = effective_salary - total_paid_this_month

        if effective_salary and remaining >= 0 and amount > remaining:
            flash(f"Cannot pay more than remaining salary for this month ({remaining:.2f} ৳).")
            return redirect(url_for("staffs"))

        bank_entry = None
        if source == "bank":
            bank_credits = db.session.query(
                db.func.sum(BalanceEntry.credit)
            ).scalar() or 0
            bank_debits = db.session.query(
                db.func.sum(BalanceEntry.debit)
            ).scalar() or 0
            current_bank_balance = bank_credits - bank_debits

            if amount > current_bank_balance:
                flash("Not enough balance in bank to pay this amount.")
                return redirect(url_for("staffs"))

            bank_entry = BalanceEntry(
                date=pay_date,
                description=f"Salary paid to {staff.name} from bank",
                credit=0,
                debit=amount,
                created_by_id=g.user.id,
            )
            db.session.add(bank_entry)

        elif source == "cash":
            pass
        else:
            flash("Invalid payment source.")
            return redirect(url_for("staffs"))

        expense_desc = note or f"Salary payment to {staff.name}"
        expense = Expense(
            date=pay_date,
            category="Salary",
            description=expense_desc,
            amount=amount,
            created_by_id=g.user.id,
        )
        db.session.add(expense)
        db.session.flush()

        payment = StaffPayment(
            staff_id=staff.id,
            date=pay_date,
            amount=amount,
            source=source,
            note=note,
            expense_id=expense.id,
            bank_entry_id=bank_entry.id if bank_entry else None,
            created_by_id=g.user.id,
        )
        db.session.add(payment)
        db.session.commit()

        if bank_entry:
            recalc_bank_balances()

        flash("Salary payment recorded.")
        return redirect(url_for("staffs"))

    # ---------- GET: list active staff ----------
    staffs_qs = Staff.query.filter_by(active=True).order_by(Staff.id).all()
    rows = []
    
    # Get month/year for salary lookup
    filter_year = filter_start.year
    filter_month_num = filter_start.month
    
    for s in staffs_qs:
        # Get effective salary for this month (may be from a previous month's change)
        effective_salary, salary_record = get_staff_salary_for_month(s, filter_year, filter_month_num)
        
        # Total paid in the selected month only
        total_paid_month = s.payments.filter(
            StaffPayment.date >= filter_start,
            StaffPayment.date <= filter_end
        ).with_entities(
            db.func.sum(StaffPayment.amount)
        ).scalar() or 0

        # All payments in the selected month (for display under each staff)
        payments_this_month = s.payments.filter(
            StaffPayment.date >= filter_start,
            StaffPayment.date <= filter_end
        ).order_by(
            StaffPayment.date.desc(), StaffPayment.id.desc()
        ).all()
        
        last_date = payments_this_month[0].date if payments_this_month else None

        # Balance = effective monthly salary - paid this month
        balance = effective_salary - total_paid_month

        rows.append(
            {
                "staff": s,
                "effective_salary": effective_salary,
                "has_override": salary_record is not None,
                "override_note": salary_record.note if salary_record else None,
                "override_from": f"{salary_record.month:02d}/{salary_record.year}" if salary_record else None,
                "total_paid": total_paid_month,
                "balance": balance,
                "last_date": last_date,
                "payments": payments_this_month,
            }
        )

    return render_template(
        "staffs.html",
        rows=rows,
        filter_month=filter_month_str,
        filter_start=filter_start,
        filter_end=filter_end,
        filter_year=filter_year,
        filter_month_num=filter_month_num,
    )


@app.route("/staffs/<int:staff_id>/delete", methods=["POST"])
@admin_required
def delete_staff(staff_id):
    """Soft-delete staff (active=False). History is kept."""
    staff = Staff.query.get_or_404(staff_id)

    log_delete("staff", staff.id, f"Marked staff '{staff.name}' as inactive")

    staff.active = False
    db.session.commit()
    flash("Staff removed from salary list (history kept).")
    return redirect(url_for("staffs"))


@app.route("/staffs/<int:staff_id>/set-monthly-salary", methods=["POST"])
@superadmin_required
def set_monthly_salary(staff_id):
    """Set or update the salary for a specific staff member for a specific month."""
    staff = Staff.query.get_or_404(staff_id)
    
    year = request.form.get("year")
    month = request.form.get("month")
    salary_str = request.form.get("salary") or "0"
    note = (request.form.get("note") or "").strip()
    action = request.form.get("action")  # "set" or "reset"
    
    try:
        year = int(year)
        month = int(month)
    except (TypeError, ValueError):
        flash("Invalid year or month.")
        return redirect(url_for("staffs"))
    
    if month < 1 or month > 12:
        flash("Invalid month.")
        return redirect(url_for("staffs"))
    
    # Find existing override
    existing = StaffMonthlySalary.query.filter_by(
        staff_id=staff.id,
        year=year,
        month=month
    ).first()
    
    if action == "reset":
        # Remove the override, revert to base salary
        if existing:
            db.session.delete(existing)
            db.session.commit()
            flash(f"Salary for {staff.name} reset to base salary (৳{staff.salary:.2f}) for this month.")
        else:
            flash("No monthly salary override to reset.")
        return redirect(url_for("staffs", month=f"{year:04d}-{month:02d}"))
    
    # Set or update salary
    try:
        salary = float(salary_str)
    except ValueError:
        flash("Invalid salary amount.")
        return redirect(url_for("staffs"))
    
    if salary < 0:
        flash("Salary cannot be negative.")
        return redirect(url_for("staffs"))
    
    if existing:
        existing.salary = salary
        existing.note = note
    else:
        new_override = StaffMonthlySalary(
            staff_id=staff.id,
            year=year,
            month=month,
            salary=salary,
            note=note
        )
        db.session.add(new_override)
    
    db.session.commit()
    
    change_type = "increased" if salary > (staff.salary or 0) else "decreased" if salary < (staff.salary or 0) else "set"
    flash(f"Salary for {staff.name} {change_type} to ৳{salary:.2f} from {month:02d}/{year} onwards.")
    return redirect(url_for("staffs", month=f"{year:04d}-{month:02d}"))


@app.route("/staffs/<int:staff_id>/history")
@login_required
def staff_history(staff_id):
    staff = Staff.query.get_or_404(staff_id)
    payments = StaffPayment.query.filter_by(staff_id=staff.id).order_by(
        StaffPayment.date, StaffPayment.id
    ).all()
    total_paid = sum(p.amount or 0 for p in payments)

    return render_template(
        "staff_history.html",
        staff=staff,
        payments=payments,
        total_paid=total_paid,
    )


@app.route("/staffs/payment/<int:payment_id>/update", methods=["POST"])
@admin_required
def update_staff_payment(payment_id):
    """
    Admin: edit a single salary payment's amount and note.
    Linked Expense and BankEntry (if any) are updated to match.
    """
    payment = StaffPayment.query.get_or_404(payment_id)
    staff = payment.staff

    amount_str = request.form.get("amount") or "0"
    note = (request.form.get("note") or "").strip()

    try:
        new_amount = float(amount_str)
    except ValueError:
        flash("Invalid amount.")
        return redirect(url_for("staff_history", staff_id=payment.staff_id))

    if new_amount <= 0:
        flash("Amount must be positive.")
        return redirect(url_for("staff_history", staff_id=payment.staff_id))

    payment.amount = new_amount
    payment.note = note

    if payment.expense_id:
        exp = Expense.query.get(payment.expense_id)
        if exp:
            exp.amount = new_amount
            if note:
                exp.description = note

    touched_bank = False
    if payment.bank_entry_id:
        be = BalanceEntry.query.get(payment.bank_entry_id)
        if be:
            be.debit = new_amount
            if note:
                be.description = f"Salary paid to {staff.name} from bank ({note})"
            touched_bank = True

    db.session.commit()

    if touched_bank:
        recalc_bank_balances()

    flash("Salary payment updated.")
    return redirect(url_for("staff_history", staff_id=payment.staff_id))


@app.route("/staffs/payment/<int:payment_id>/delete", methods=["POST"])
@admin_required
def delete_staff_payment(payment_id):
    """
    Admin: delete a salary payment and its linked Expense / BankEntry.
    """
    payment = StaffPayment.query.get_or_404(payment_id)
    staff = payment.staff

    log_delete(
        "staff_payment",
        payment.id,
        f"Deleted salary payment to {staff.name} {payment.amount:.2f} BDT on {payment.date}",
    )

    touched_bank = False

    if payment.bank_entry_id:
        be = BalanceEntry.query.get(payment.bank_entry_id)
        if be:
            db.session.delete(be)
            touched_bank = True

    if payment.expense_id:
        exp = Expense.query.get(payment.expense_id)
        if exp:
            db.session.delete(exp)

    db.session.delete(payment)
    db.session.commit()

    if touched_bank:
        recalc_bank_balances()

    flash("Salary payment deleted.")
    return redirect(url_for("staff_history", staff_id=payment.staff_id))


# ---------- Staff salary statements ----------

@app.route("/staffs/statement/staff", methods=["POST"])
@login_required
def staff_salary_statement():
    """Printable salary statement for a single staff and month."""
    staff_id = request.form.get("staff_id")
    month_str = request.form.get("month")  # YYYY-MM

    try:
        staff_id = int(staff_id)
    except (TypeError, ValueError):
        flash("Invalid staff selected for statement.")
        return redirect(url_for("staffs"))

    staff = Staff.query.get_or_404(staff_id)

    if not month_str:
        flash("Please select a month.")
        return redirect(url_for("staffs"))

    try:
        start_dt = datetime.strptime(month_str + "-01", "%Y-%m-%d")
    except ValueError:
        flash("Invalid month format.")
        return redirect(url_for("staffs"))

    year = start_dt.year
    month = start_dt.month

    if month == 12:
        next_month_dt = datetime(year + 1, 1, 1)
    else:
        next_month_dt = datetime(year, month + 1, 1)

    start_date = start_dt.date()
    end_date = (next_month_dt - timedelta(days=1)).date()

    payments = StaffPayment.query.filter(
        StaffPayment.staff_id == staff.id,
        StaffPayment.date >= start_date,
        StaffPayment.date <= end_date,
    ).order_by(StaffPayment.date, StaffPayment.id).all()

    total_paid = sum(p.amount or 0 for p in payments)
    
    # Get effective salary for this month (may be from a previous month's change)
    effective_salary, salary_record = get_staff_salary_for_month(staff, year, month)
    base_salary = staff.salary or 0
    has_override = salary_record is not None
    
    remaining = effective_salary - total_paid

    return render_template(
        "staff_salary_statement.html",
        staff=staff,
        year=year,
        month=month,
        start_date=start_date,
        end_date=end_date,
        payments=payments,
        total_paid=total_paid,
        salary=effective_salary,
        base_salary=base_salary,
        has_override=has_override,
        remaining=remaining,
        timestamp=datetime.now(),
    )


@app.route("/staffs/statement/all", methods=["POST"])
@login_required
def staff_salary_statement_all():
    """Printable salary statement for all staff for a month."""
    month_str = request.form.get("month")

    if not month_str:
        flash("Please select a month.")
        return redirect(url_for("staffs"))

    try:
        start_dt = datetime.strptime(month_str + "-01", "%Y-%m-%d")
    except ValueError:
        flash("Invalid month format.")
        return redirect(url_for("staffs"))

    year = start_dt.year
    month = start_dt.month

    if month == 12:
        next_month_dt = datetime(year + 1, 1, 1)
    else:
        next_month_dt = datetime(year, month + 1, 1)

    start_date = start_dt.date()
    end_date = (next_month_dt - timedelta(days=1)).date()

    staffs_qs = Staff.query.filter_by(active=True).order_by(Staff.id).all()

    rows = []
    total_paid_all = 0
    total_salary_all = 0

    for s in staffs_qs:
        # Get effective salary for this month
        effective_salary, salary_record = get_staff_salary_for_month(s, year, month)
        
        payments = StaffPayment.query.filter(
            StaffPayment.staff_id == s.id,
            StaffPayment.date >= start_date,
            StaffPayment.date <= end_date,
        ).all()
        paid = sum(p.amount or 0 for p in payments)
        balance = effective_salary - paid
        total_paid_all += paid
        total_salary_all += effective_salary
        rows.append(
            {
                "staff": s,
                "salary": effective_salary,
                "has_override": salary_record is not None,
                "paid": paid,
                "balance": balance,
            }
        )

    return render_template(
        "staff_salary_statement_all.html",
        year=year,
        month=month,
        start_date=start_date,
        end_date=end_date,
        rows=rows,
        total_paid_all=total_paid_all,
        total_salary_all=total_salary_all,
        timestamp=datetime.now(),
    )


# ============================================================
#  Suppliers
# ============================================================

@app.route("/suppliers", methods=["GET", "POST"])
@login_required
def suppliers():
    """
    Supplier payment page:
      - add suppliers with total_due   (admin only)
      - pay them part by part (cash or bank)
    """
    form_type = request.form.get("form_type") if request.method == "POST" else None
    today = datetime.today().date()

    # ---------- Add new supplier (admin only) ----------
    if request.method == "POST" and form_type == "add_supplier":
        if g.user.role != "admin":
            flash("Only admin can add suppliers.")
            return redirect(url_for("suppliers"))

        name = (request.form.get("name") or "").strip()
        details = (request.form.get("details") or "").strip()

        if not name:
            flash("Supplier name is required.")
            return redirect(url_for("suppliers"))

        # total_due starts at 0, will be calculated from bills
        supplier = Supplier(name=name, details=details, total_due=0)
        db.session.add(supplier)
        db.session.commit()
        flash("Supplier added. Now add bills from the supplier's detail page.")
        return redirect(url_for("supplier_detail", supplier_id=supplier.id))

    # ---------- Pay supplier (partial) ----------
    if request.method == "POST" and form_type == "pay_supplier":
        supplier_id = request.form.get("supplier_id")
        source = request.form.get("source")  # "cash" or "bank"
        amount_str = request.form.get("amount") or "0"
        note = request.form.get("note") or ""
        date_str = request.form.get("date")
        redirect_to_detail = request.form.get("redirect_to_detail") == "1"

        try:
            supplier_id = int(supplier_id)
        except (TypeError, ValueError):
            flash("Invalid supplier selection.")
            return redirect(url_for("suppliers"))

        supplier = Supplier.query.get_or_404(supplier_id)
        
        def redirect_back():
            if redirect_to_detail:
                return redirect(url_for("supplier_detail", supplier_id=supplier_id))
            return redirect(url_for("suppliers"))

        try:
            amount = float(amount_str)
        except ValueError:
            flash("Invalid amount.")
            return redirect_back()

        if amount <= 0:
            flash("Amount must be positive.")
            return redirect_back()

        if date_str:
            try:
                pay_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            except ValueError:
                flash("Invalid date format.")
                return redirect_back()
        else:
            pay_date = today

        total_paid_so_far = supplier.payments.with_entities(
            db.func.sum(SupplierPayment.amount)
        ).scalar() or 0
        remaining = (supplier.total_due or 0) - total_paid_so_far

        if supplier.total_due and remaining >= 0 and amount > remaining:
            flash(f"Cannot pay more than remaining amount ({remaining:.2f} ৳).")
            return redirect_back()

        bank_entry = None
        if source == "bank":
            bank_credits = db.session.query(
                db.func.sum(BalanceEntry.credit)
            ).scalar() or 0
            bank_debits = db.session.query(
                db.func.sum(BalanceEntry.debit)
            ).scalar() or 0
            current_bank_balance = bank_credits - bank_debits

            if amount > current_bank_balance:
                flash("Not enough balance in bank to pay this amount.")
                return redirect_back()

            bank_entry = BalanceEntry(
                date=pay_date,
                description=f"Payment to supplier {supplier.name} from bank",
                credit=0,
                debit=amount,
                created_by_id=g.user.id,
            )
            db.session.add(bank_entry)

        elif source == "cash":
            pass
        else:
            flash("Invalid payment source.")
            return redirect_back()

        expense_desc = note or f"Payment to supplier {supplier.name}"
        expense = Expense(
            date=pay_date,
            category="Supplier",
            description=expense_desc,
            amount=amount,
            created_by_id=g.user.id,
        )
        db.session.add(expense)
        db.session.flush()

        payment = SupplierPayment(
            supplier_id=supplier.id,
            date=pay_date,
            amount=amount,
            source=source,
            note=note,
            expense_id=expense.id,
            bank_entry_id=bank_entry.id if bank_entry else None,
            created_by_id=g.user.id,
        )
        db.session.add(payment)
        db.session.commit()

        if bank_entry:
            recalc_bank_balances()

        flash("Supplier payment recorded.")
        return redirect_back()

    # ---------- GET: list suppliers ----------
    suppliers_qs = Supplier.query.order_by(Supplier.id).all()
    rows = []
    for s in suppliers_qs:
        total_paid = s.payments.with_entities(
            db.func.sum(SupplierPayment.amount)
        ).scalar() or 0

        last_payment = s.payments.order_by(
            SupplierPayment.date.desc(), SupplierPayment.id.desc()
        ).first()
        last_date = last_payment.date if last_payment else None

        balance = (s.total_due or 0) - total_paid

        rows.append(
            {
                "supplier": s,
                "total_paid": total_paid,
                "balance": balance,
                "last_date": last_date,
            }
        )

    return render_template("suppliers.html", rows=rows)


@app.route("/suppliers/<int:supplier_id>/delete", methods=["POST"])
@admin_required
def delete_supplier(supplier_id):
    """
    HARD delete supplier: delete supplier, all their bills, payments,
    and linked expenses / bank entries.
    """
    supplier = Supplier.query.get_or_404(supplier_id)

    log_delete("supplier", supplier.id, f"Deleted supplier '{supplier.name}' and all bills/payments")

    touched_bank = False

    # Delete all bills
    SupplierBill.query.filter_by(supplier_id=supplier.id).delete()

    # Delete all payments and linked entries
    payments = SupplierPayment.query.filter_by(supplier_id=supplier.id).all()
    for p in payments:
        if p.bank_entry_id:
            be = BalanceEntry.query.get(p.bank_entry_id)
            if be:
                db.session.delete(be)
                touched_bank = True
        if p.expense_id:
            exp = Expense.query.get(p.expense_id)
            if exp:
                db.session.delete(exp)
        db.session.delete(p)

    db.session.delete(supplier)
    db.session.commit()

    if touched_bank:
        recalc_bank_balances()

    flash("Supplier and all related bills/payments deleted.")
    return redirect(url_for("suppliers"))


@app.route("/suppliers/<int:supplier_id>", methods=["GET", "POST"])
@login_required
def supplier_detail(supplier_id):
    """
    Supplier detail page:
      - View supplier info, bills, and payments
      - Add bills to increase total payable
    """
    supplier = Supplier.query.get_or_404(supplier_id)
    today = datetime.today().date()

    # Handle adding a new bill
    if request.method == "POST":
        form_type = request.form.get("form_type")

        if form_type == "add_bill":
            date_str = request.form.get("date")
            description = (request.form.get("description") or "").strip()
            amount_str = request.form.get("amount") or "0"

            try:
                amount = float(amount_str)
            except ValueError:
                flash("Invalid bill amount.")
                return redirect(url_for("supplier_detail", supplier_id=supplier_id))

            if amount <= 0:
                flash("Bill amount must be positive.")
                return redirect(url_for("supplier_detail", supplier_id=supplier_id))

            if date_str:
                try:
                    bill_date = datetime.strptime(date_str, "%Y-%m-%d").date()
                except ValueError:
                    flash("Invalid date format.")
                    return redirect(url_for("supplier_detail", supplier_id=supplier_id))
            else:
                bill_date = today

            bill = SupplierBill(
                supplier_id=supplier.id,
                date=bill_date,
                description=description,
                amount=amount,
                created_by_id=g.user.id,
            )
            db.session.add(bill)

            # Recalculate total_due
            supplier.recalc_total_due()
            db.session.commit()

            flash(f"Bill of ৳{amount:.2f} added successfully.")
            return redirect(url_for("supplier_detail", supplier_id=supplier_id))

        elif form_type == "update_details":
            # Allow admin to update supplier details
            if g.user.role not in ("admin", "superadmin"):
                flash("Only admin can update supplier details.")
                return redirect(url_for("supplier_detail", supplier_id=supplier_id))

            new_name = (request.form.get("name") or "").strip()
            new_details = (request.form.get("details") or "").strip()

            if new_name:
                supplier.name = new_name
            supplier.details = new_details
            db.session.commit()

            flash("Supplier details updated.")
            return redirect(url_for("supplier_detail", supplier_id=supplier_id))

    # Get all bills and payments
    bills = SupplierBill.query.filter_by(supplier_id=supplier.id).order_by(
        SupplierBill.date.desc(), SupplierBill.id.desc()
    ).all()

    payments = SupplierPayment.query.filter_by(supplier_id=supplier.id).order_by(
        SupplierPayment.date.desc(), SupplierPayment.id.desc()
    ).all()

    total_bills = sum(b.amount or 0 for b in bills)
    total_paid = sum(p.amount or 0 for p in payments)
    balance = total_bills - total_paid

    return render_template(
        "supplier_detail.html",
        supplier=supplier,
        bills=bills,
        payments=payments,
        total_bills=total_bills,
        total_paid=total_paid,
        balance=balance,
    )


@app.route("/suppliers/<int:supplier_id>/report", methods=["POST"])
@login_required
def supplier_report(supplier_id):
    """Generate a printable report for a supplier within a date range."""
    supplier = Supplier.query.get_or_404(supplier_id)

    start_str = request.form.get("start_date")
    end_str = request.form.get("end_date")

    try:
        start_date = datetime.strptime(start_str, "%Y-%m-%d").date()
        end_date = datetime.strptime(end_str, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        flash("Invalid dates for report.")
        return redirect(url_for("supplier_detail", supplier_id=supplier_id))

    if end_date < start_date:
        flash("End date cannot be earlier than start date.")
        return redirect(url_for("supplier_detail", supplier_id=supplier_id))

    # Get bills in date range
    bills = SupplierBill.query.filter(
        SupplierBill.supplier_id == supplier.id,
        SupplierBill.date >= start_date,
        SupplierBill.date <= end_date,
    ).order_by(SupplierBill.date, SupplierBill.id).all()

    # Get payments in date range
    payments = SupplierPayment.query.filter(
        SupplierPayment.supplier_id == supplier.id,
        SupplierPayment.date >= start_date,
        SupplierPayment.date <= end_date,
    ).order_by(SupplierPayment.date, SupplierPayment.id).all()

    # Calculate totals for this period
    total_bills_period = sum(b.amount or 0 for b in bills)
    total_paid_period = sum(p.amount or 0 for p in payments)

    # Calculate all-time totals
    total_bills_all = supplier.bills.with_entities(
        db.func.sum(SupplierBill.amount)
    ).scalar() or 0
    total_paid_all = supplier.payments.with_entities(
        db.func.sum(SupplierPayment.amount)
    ).scalar() or 0
    balance_all = total_bills_all - total_paid_all

    # Opening balance (before start date)
    bills_before = supplier.bills.filter(
        SupplierBill.date < start_date
    ).with_entities(db.func.sum(SupplierBill.amount)).scalar() or 0

    paid_before = supplier.payments.filter(
        SupplierPayment.date < start_date
    ).with_entities(db.func.sum(SupplierPayment.amount)).scalar() or 0

    opening_balance = bills_before - paid_before
    closing_balance = opening_balance + total_bills_period - total_paid_period

    return render_template(
        "supplier_report.html",
        supplier=supplier,
        start_date=start_date,
        end_date=end_date,
        bills=bills,
        payments=payments,
        total_bills_period=total_bills_period,
        total_paid_period=total_paid_period,
        opening_balance=opening_balance,
        closing_balance=closing_balance,
        total_bills_all=total_bills_all,
        total_paid_all=total_paid_all,
        balance_all=balance_all,
        timestamp=datetime.now(),
    )


@app.route("/suppliers/<int:supplier_id>/bill/<int:bill_id>/delete", methods=["POST"])
@admin_required
def delete_supplier_bill(supplier_id, bill_id):
    """Delete a supplier bill and recalculate total_due."""
    bill = SupplierBill.query.get_or_404(bill_id)
    supplier = Supplier.query.get_or_404(supplier_id)

    if bill.supplier_id != supplier_id:
        flash("Invalid bill.")
        return redirect(url_for("supplier_detail", supplier_id=supplier_id))

    log_delete(
        "supplier_bill",
        bill.id,
        f"Deleted bill for {supplier.name}: {bill.description} ৳{bill.amount:.2f}",
    )

    db.session.delete(bill)
    supplier.recalc_total_due()
    db.session.commit()

    flash("Bill deleted.")
    return redirect(url_for("supplier_detail", supplier_id=supplier_id))


@app.route("/suppliers/<int:supplier_id>/history")
@login_required
def supplier_history(supplier_id):
    supplier = Supplier.query.get_or_404(supplier_id)
    payments = SupplierPayment.query.filter_by(supplier_id=supplier.id).order_by(
        SupplierPayment.date, SupplierPayment.id
    ).all()
    total_paid = sum(p.amount or 0 for p in payments)

    return render_template(
        "supplier_history.html",
        supplier=supplier,
        payments=payments,
        total_paid=total_paid,
    )


@app.route("/suppliers/payment/<int:payment_id>/update", methods=["POST"])
@admin_required
def update_supplier_payment(payment_id):
    """
    Admin: edit a single supplier payment's amount and note.
    Linked Expense and BankEntry (if any) are updated to match.
    """
    payment = SupplierPayment.query.get_or_404(payment_id)
    supplier = payment.supplier

    amount_str = request.form.get("amount") or "0"
    note = (request.form.get("note") or "").strip()

    try:
        new_amount = float(amount_str)
    except ValueError:
        flash("Invalid amount.")
        return redirect(url_for("supplier_history", supplier_id=payment.supplier_id))

    if new_amount <= 0:
        flash("Amount must be positive.")
        return redirect(url_for("supplier_history", supplier_id=payment.supplier_id))

    payment.amount = new_amount
    payment.note = note

    if payment.expense_id:
        exp = Expense.query.get(payment.expense_id)
        if exp:
            exp.amount = new_amount
            if note:
                exp.description = note

    touched_bank = False
    if payment.bank_entry_id:
        be = BalanceEntry.query.get(payment.bank_entry_id)
        if be:
            be.debit = new_amount
            if note:
                be.description = f"Payment to supplier {supplier.name} from bank ({note})"
            touched_bank = True

    db.session.commit()

    if touched_bank:
        recalc_bank_balances()

    flash("Supplier payment updated.")
    return redirect(url_for("supplier_history", supplier_id=payment.supplier_id))


@app.route("/suppliers/payment/<int:payment_id>/delete", methods=["POST"])
@admin_required
def delete_supplier_payment(payment_id):
    """
    Admin: delete a supplier payment and its linked Expense / BankEntry.
    """
    payment = SupplierPayment.query.get_or_404(payment_id)
    supplier = payment.supplier

    log_delete(
        "supplier_payment",
        payment.id,
        f"Deleted supplier payment to {supplier.name} {payment.amount:.2f} BDT on {payment.date}",
    )

    touched_bank = False

    if payment.bank_entry_id:
        be = BalanceEntry.query.get(payment.bank_entry_id)
        if be:
            db.session.delete(be)
            touched_bank = True

    if payment.expense_id:
        exp = Expense.query.get(payment.expense_id)
        if exp:
            db.session.delete(exp)

    db.session.delete(payment)
    db.session.commit()

    if touched_bank:
        recalc_bank_balances()

    flash("Supplier payment deleted.")
    return redirect(url_for("supplier_history", supplier_id=payment.supplier_id))


@app.route("/suppliers/statement", methods=["POST"])
@login_required
def supplier_statement():
    """Printable statement for a supplier over a date range."""
    supplier_id = request.form.get("supplier_id")
    start_str = request.form.get("start_date")
    end_str = request.form.get("end_date")

    try:
        supplier_id = int(supplier_id)
    except (TypeError, ValueError):
        flash("Invalid supplier selected for statement.")
        return redirect(url_for("suppliers"))

    supplier = Supplier.query.get_or_404(supplier_id)

    if not start_str or not end_str:
        flash("Please select both start and end dates.")
        return redirect(url_for("suppliers"))

    try:
        start_date = datetime.strptime(start_str, "%Y-%m-%d").date()
        end_date = datetime.strptime(end_str, "%Y-%m-%d").date()
    except ValueError:
        flash("Invalid dates.")
        return redirect(url_for("suppliers"))

    if end_date < start_date:
        flash("End date cannot be earlier than start date.")
        return redirect(url_for("suppliers"))

    payments = SupplierPayment.query.filter(
        SupplierPayment.supplier_id == supplier.id,
        SupplierPayment.date >= start_date,
        SupplierPayment.date <= end_date,
    ).order_by(SupplierPayment.date, SupplierPayment.id).all()

    total_paid_range = sum(p.amount or 0 for p in payments)
    total_paid_all = supplier.payments.with_entities(
        db.func.sum(SupplierPayment.amount)
    ).scalar() or 0
    balance_total = (supplier.total_due or 0) - total_paid_all

    return render_template(
        "supplier_statement.html",
        supplier=supplier,
        start_date=start_date,
        end_date=end_date,
        payments=payments,
        total_paid_range=total_paid_range,
        total_paid_all=total_paid_all,
        balance_total=balance_total,
        timestamp=datetime.now(),
    )


# ============================================================
#  Expense Templates (Create Expense)
# ============================================================

@app.route("/expense-templates", methods=["GET", "POST"])
@login_required
def expense_templates():
    """
    Create / manage reusable expense types.
    """
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        default_description = (request.form.get("default_description") or "").strip()
        default_amount_str = request.form.get("default_amount") or "0"

        if not name:
            flash("Expense name is required.")
            return redirect(url_for("expense_templates"))

        try:
            default_amount = float(default_amount_str)
        except ValueError:
            flash("Invalid default amount.")
            return redirect(url_for("expense_templates"))

        existing = ExpenseTemplate.query.filter_by(name=name).first()
        if existing:
            flash("An expense with this name already exists.")
            return redirect(url_for("expense_templates"))

        t = ExpenseTemplate(
            name=name,
            default_description=default_description,
            default_amount=default_amount,
        )
        db.session.add(t)
        db.session.commit()
        flash("Expense template added.")
        return redirect(url_for("expense_templates"))

    templates = ExpenseTemplate.query.order_by(ExpenseTemplate.name).all()

    return render_template("expense_templates.html", templates=templates)


@app.route("/expense-templates/<int:template_id>/delete", methods=["POST"])
@admin_required
def delete_expense_template(template_id):
    """
    Hard-delete an expense template (so the same name can be reused later).
    """
    t = ExpenseTemplate.query.get_or_404(template_id)
    name = t.name

    log_delete("expense_template", t.id, f"Deleted expense template '{name}'")

    db.session.delete(t)
    db.session.commit()

    flash("Expense template removed.")
    return redirect(url_for("expense_templates"))


# ============================================================
#  Delete History
# ============================================================

@app.route("/delete-history")
@admin_required
def delete_history():
    logs = DeleteLog.query.order_by(DeleteLog.timestamp.desc()).limit(500).all()
    return render_template("delete_history.html", logs=logs)


# ============================================================
#  Auth Routes
# ============================================================

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        user = User.query.filter_by(username=username).first()
        if user and user.check_password(password):
            session.clear()
            session["user_id"] = user.id
            flash("Logged in successfully.")
            next_url = request.args.get("next")
            return redirect(next_url or url_for("dashboard"))

        flash("Invalid username or password.")

    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    session.clear()
    flash("You have been logged out.")
    return redirect(url_for("login"))


# ============================================================
#  SuperAdmin Panel
# ============================================================

@app.route("/superadmin")
@superadmin_required
def superadmin_panel():
    """SuperAdmin panel with various admin tools."""
    return render_template("superadmin_panel.html")


@app.route("/superadmin/edit-expense", methods=["GET", "POST"])
@superadmin_required
def edit_expense():
    """Edit expenses for a specific date."""
    selected_date = None
    expenses = []
    
    if request.method == "POST":
        form_type = request.form.get("form_type")
        
        if form_type == "select_date":
            # User selected a date to view expenses
            date_str = request.form.get("date")
            if date_str:
                try:
                    selected_date = datetime.strptime(date_str, "%Y-%m-%d").date()
                    expenses = Expense.query.filter_by(date=selected_date).order_by(Expense.id).all()
                except ValueError:
                    flash("Invalid date format.")
            else:
                flash("Please select a date.")
                
        elif form_type == "save_expenses":
            # User is saving edited expenses
            date_str = request.form.get("expense_date")
            if date_str:
                try:
                    selected_date = datetime.strptime(date_str, "%Y-%m-%d").date()
                except ValueError:
                    flash("Invalid date.")
                    return redirect(url_for("edit_expense"))
            
            # Get all expense data from form
            expense_ids = request.form.getlist("expense_id[]")
            categories = request.form.getlist("category[]")
            descriptions = request.form.getlist("description[]")
            amounts = request.form.getlist("amount[]")
            delete_flags = request.form.getlist("delete[]")
            
            updated_count = 0
            deleted_count = 0
            
            for i, exp_id in enumerate(expense_ids):
                try:
                    exp_id_int = int(exp_id)
                except ValueError:
                    continue
                
                expense = Expense.query.get(exp_id_int)
                if not expense:
                    continue
                
                # Check if marked for deletion
                if exp_id in delete_flags:
                    log_delete(
                        "expense",
                        expense.id,
                        f"Deleted expense: {expense.category} - {expense.description} ৳{expense.amount:.2f}"
                    )
                    db.session.delete(expense)
                    deleted_count += 1
                    continue
                
                # Update expense
                category = (categories[i] if i < len(categories) else "").strip()
                description = (descriptions[i] if i < len(descriptions) else "").strip()
                amount_str = amounts[i] if i < len(amounts) else "0"
                
                try:
                    amount = float(amount_str or 0)
                except ValueError:
                    amount = expense.amount
                
                # Track if anything changed
                changed = False
                old_amount = expense.amount
                
                if category and category != expense.category:
                    expense.category = category
                    changed = True
                if description != expense.description:
                    expense.description = description
                    changed = True
                if amount > 0 and amount != expense.amount:
                    old_amt = expense.amount
                    expense.amount = amount
                    changed = True
                    
                    # Also update linked StaffPayment if exists
                    linked_staff_payment = StaffPayment.query.filter_by(expense_id=expense.id).first()
                    if linked_staff_payment:
                        linked_staff_payment.amount = amount
                        flash(f"DEBUG: Updated StaffPayment #{linked_staff_payment.id} from {old_amt} to {amount}")
                        # If paid from bank, also update the bank entry
                        if linked_staff_payment.bank_entry_id:
                            bank_entry = BalanceEntry.query.get(linked_staff_payment.bank_entry_id)
                            if bank_entry:
                                bank_entry.debit = amount
                                flash(f"DEBUG: Updated BankEntry #{bank_entry.id}")
                    else:
                        # Check if this is a Salary expense but no linked payment found
                        if expense.category == "Salary":
                            flash(f"DEBUG: Salary expense #{expense.id} has NO linked StaffPayment!")
                    
                    # Also update linked SupplierPayment if exists
                    linked_supplier_payment = SupplierPayment.query.filter_by(expense_id=expense.id).first()
                    if linked_supplier_payment:
                        linked_supplier_payment.amount = amount
                        flash(f"DEBUG: Updated SupplierPayment #{linked_supplier_payment.id} from {old_amt} to {amount}")
                        # If paid from bank, also update the bank entry
                        if linked_supplier_payment.bank_entry_id:
                            bank_entry = BalanceEntry.query.get(linked_supplier_payment.bank_entry_id)
                            if bank_entry:
                                bank_entry.debit = amount
                    else:
                        if expense.category == "Supplier":
                            flash(f"DEBUG: Supplier expense #{expense.id} has NO linked SupplierPayment!")
                
                if changed:
                    updated_count += 1
            
            db.session.commit()
            
            # Recalculate bank balances if any bank entries were affected
            recalc_bank_balances()
            
            if deleted_count > 0:
                flash(f"Deleted {deleted_count} expense(s).")
            if updated_count > 0:
                flash(f"Updated {updated_count} expense(s). All linked records updated.")
            if updated_count == 0 and deleted_count == 0:
                flash("No changes detected.")
            
            # Reload expenses for the date
            expenses = Expense.query.filter_by(date=selected_date).order_by(Expense.id).all()
    
    templates = ExpenseTemplate.query.order_by(ExpenseTemplate.name).all()
    return render_template(
        "edit_expense.html",
        selected_date=selected_date,
        expenses=expenses,
        templates=templates
    )


@app.route("/bulk-expense", methods=["GET", "POST"])
@login_required
def bulk_expense():
    """Add multiple expenses at once."""
    if request.method == "POST":
        date_str = request.form.get("date")
        
        # Parse date
        if date_str:
            try:
                expense_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            except ValueError:
                flash("Invalid date format.")
                return redirect(url_for("bulk_expense"))
        else:
            expense_date = datetime.today().date()
        
        # Get all expense entries from the form
        categories = request.form.getlist("category[]")
        descriptions = request.form.getlist("description[]")
        amounts = request.form.getlist("amount[]")
        
        if not categories:
            flash("No expenses to add.")
            return redirect(url_for("bulk_expense"))
        
        added_count = 0
        total_amount = 0
        
        for i in range(len(categories)):
            category = (categories[i] or "").strip()
            description = (descriptions[i] if i < len(descriptions) else "").strip()
            amount_str = amounts[i] if i < len(amounts) else "0"
            
            # Skip empty rows
            if not category and not amount_str:
                continue
            
            try:
                amount = float(amount_str or 0)
            except ValueError:
                amount = 0
            
            if amount <= 0:
                continue
            
            if not category:
                category = "General"
            
            exp = Expense(
                date=expense_date,
                category=category,
                description=description,
                amount=amount,
                created_by_id=g.user.id,
            )
            db.session.add(exp)
            added_count += 1
            total_amount += amount
        
        if added_count > 0:
            db.session.commit()
            flash(f"Successfully added {added_count} expense(s) totaling ৳{total_amount:.2f}")
        else:
            flash("No valid expenses to add.")
        
        return redirect(url_for("bulk_expense"))
    
    # GET: show the form
    templates = ExpenseTemplate.query.order_by(ExpenseTemplate.name).all()
    return render_template("bulk_expense.html", templates=templates)


# ============================================================
#  Database Init & Tiny Migrations
# ============================================================

with app.app_context():
    # Create all tables if not exist
    db.create_all()

    # -------------------------
    # Ensure default SUPERADMIN
    # -------------------------
    superadmin = User.query.filter_by(username="dp_mamun").first()
    if not superadmin:
        superadmin = User(username="dp_mamun", role="superadmin")
        superadmin.set_password("supersecret")  # change after first login
        db.session.add(superadmin)
        db.session.commit()
        
    else:
        if superadmin.role != "superadmin":
            superadmin.role = "superadmin"
            db.session.commit()
            print("Updated existing 'dp_mamun' to SUPERADMIN")

    # -------------------------
    # Ensure default staff
    # -------------------------
    staff_user = User.query.filter_by(username="staff").first()
    if not staff_user:
        staff_user = User(username="staff", role="employee")
        staff_user.set_password("staff123")
        db.session.add(staff_user)
        db.session.commit()
        
    else:
        if staff_user.role != "employee":
            staff_user.role = "employee"
            db.session.commit()
            print("Updated existing 'staff' to EMPLOYEE")

    # -------------------------
    # Tiny auto-migrations for old DBs
    # -------------------------
    try:
        db.session.execute(text("SELECT role FROM users LIMIT 1"))
        db.session.commit()
    except Exception:
        try:
            db.session.execute(
                text("ALTER TABLE users ADD COLUMN role VARCHAR(20) NOT NULL DEFAULT 'employee'")
            )
            db.session.commit()
            print("Added 'role' column to users table")
        except Exception:
            db.session.rollback()

    # days.created_at
    try:
        db.session.execute(text("SELECT created_at FROM days LIMIT 1"))
        db.session.commit()
    except Exception:
        try:
            db.session.execute(text("ALTER TABLE days ADD COLUMN created_at DATETIME"))
            db.session.commit()
            print("Added 'created_at' column to days table")
        except Exception:
            db.session.rollback()

    # expenses.created_at
    try:
        db.session.execute(text("SELECT created_at FROM expenses LIMIT 1"))
        db.session.commit()
    except Exception:
        try:
            db.session.execute(text("ALTER TABLE expenses ADD COLUMN created_at DATETIME"))
            db.session.commit()
            print("Added 'created_at' column to expenses table")
        except Exception:
            db.session.rollback()

    # doctor_bills.created_at
    try:
        db.session.execute(text("SELECT created_at FROM doctor_bills LIMIT 1"))
        db.session.commit()
    except Exception:
        try:
            db.session.execute(text("ALTER TABLE doctor_bills ADD COLUMN created_at DATETIME"))
            db.session.commit()
            print("Added 'created_at' column to doctor_bills table")
        except Exception:
            db.session.rollback()

    # staff_payments.expense_id
    try:
        db.session.execute(text("SELECT expense_id FROM staff_payments LIMIT 1"))
        db.session.commit()
    except Exception:
        try:
            db.session.execute(text("ALTER TABLE staff_payments ADD COLUMN expense_id INTEGER"))
            db.session.commit()
            print("Added 'expense_id' column to staff_payments table")
        except Exception:
            db.session.rollback()

    # staff_payments.bank_entry_id
    try:
        db.session.execute(text("SELECT bank_entry_id FROM staff_payments LIMIT 1"))
        db.session.commit()
    except Exception:
        try:
            db.session.execute(text("ALTER TABLE staff_payments ADD COLUMN bank_entry_id INTEGER"))
            db.session.commit()
            print("Added 'bank_entry_id' column to staff_payments table")
        except Exception:
            db.session.rollback()

    # supplier_payments.expense_id and bank_entry_id
    try:
        db.session.execute(text("SELECT expense_id FROM supplier_payments LIMIT 1"))
        db.session.commit()
    except Exception:
        try:
            db.session.execute(text("ALTER TABLE supplier_payments ADD COLUMN expense_id INTEGER"))
            db.session.execute(text("ALTER TABLE supplier_payments ADD COLUMN bank_entry_id INTEGER"))
            db.session.commit()
            print("Added 'expense_id' and 'bank_entry_id' columns to supplier_payments")
        except Exception:
            db.session.rollback()

    # -------------------------
    # Add created_by_id columns to all transactional tables
    # -------------------------
    tables_needing_created_by = [
        "days",
        "expenses", 
        "doctor_bills",
        "balance_entries",
        "staff_payments",
        "supplier_bills",
        "supplier_payments",
        "lab_collections",
    ]
    
    for table in tables_needing_created_by:
        try:
            db.session.execute(text(f"SELECT created_by_id FROM {table} LIMIT 1"))
            db.session.commit()
        except Exception:
            db.session.rollback()
            try:
                db.session.execute(text(f"ALTER TABLE {table} ADD COLUMN created_by_id INTEGER"))
                db.session.commit()
                print(f"Added 'created_by_id' column to {table} table")
            except Exception:
                db.session.rollback()


# ============================================================
#  Run App (Dev)
# ============================================================

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=8080)
