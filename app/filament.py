from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required

from app import db
from app.models import Filament, FilamentSpool

filament_bp = Blueprint('filament', __name__, url_prefix='/filament')


@filament_bp.route('/')
@login_required
def index():
    filaments = Filament.query.order_by(Filament.name).all()
    total_spools = sum(f.total_spools for f in filaments)
    total_weight = sum(f.total_weight_g for f in filaments)
    return render_template(
        'filament.html',
        filaments=filaments,
        total_spools=total_spools,
        total_weight=total_weight,
    )


@filament_bp.route('/add', methods=['POST'])
@login_required
def add_filament():
    filament = Filament(
        name=request.form['name'],
        brand=request.form.get('brand', ''),
        material=request.form.get('material', ''),
        color=request.form.get('color', ''),
        cost_per_kg=float(request.form.get('cost_per_kg', 0) or 0),
        price_per_kg=float(request.form.get('price_per_kg', 0) or 0),
        notes=request.form.get('notes', ''),
    )
    db.session.add(filament)
    db.session.commit()

    if request.form.get('add_spool') == 'on':
        weight = float(request.form.get('spool_weight', 1000) or 1000)
        spool = FilamentSpool(
            filament_id=filament.id,
            initial_weight_g=weight,
            weight_remaining_g=weight,
            purchase_cost=float(request.form.get('spool_cost', 0) or 0),
            location=request.form.get('spool_location', ''),
        )
        db.session.add(spool)
        db.session.commit()

    flash('Filament added successfully')
    return redirect(url_for('filament.index'))


@filament_bp.route('/<int:filament_id>/edit', methods=['POST'])
@login_required
def edit_filament(filament_id):
    filament = Filament.query.get_or_404(filament_id)
    filament.name = request.form['name']
    filament.brand = request.form.get('brand', '')
    filament.material = request.form.get('material', '')
    filament.color = request.form.get('color', '')
    filament.cost_per_kg = float(request.form.get('cost_per_kg', 0) or 0)
    filament.price_per_kg = float(request.form.get('price_per_kg', 0) or 0)
    filament.notes = request.form.get('notes', '')
    db.session.commit()
    flash('Filament updated')
    return redirect(url_for('filament.index'))


@filament_bp.route('/<int:filament_id>/delete', methods=['POST'])
@login_required
def delete_filament(filament_id):
    filament = Filament.query.get_or_404(filament_id)
    db.session.delete(filament)
    db.session.commit()
    flash('Filament deleted')
    return redirect(url_for('filament.index'))


@filament_bp.route('/<int:filament_id>/spool/add', methods=['POST'])
@login_required
def add_spool(filament_id):
    filament = Filament.query.get_or_404(filament_id)
    weight = float(request.form.get('weight_g', 1000) or 1000)
    spool = FilamentSpool(
        filament_id=filament.id,
        initial_weight_g=weight,
        weight_remaining_g=weight,
        purchase_cost=float(request.form.get('purchase_cost', 0) or 0),
        location=request.form.get('location', ''),
        notes=request.form.get('notes', ''),
    )
    db.session.add(spool)
    db.session.commit()
    flash(f'Spool added to {filament.name}')
    return redirect(url_for('filament.index'))


@filament_bp.route('/spool/<int:spool_id>/edit', methods=['POST'])
@login_required
def edit_spool(spool_id):
    spool = FilamentSpool.query.get_or_404(spool_id)
    spool.weight_remaining_g = float(request.form.get('weight_remaining_g', spool.weight_remaining_g) or 0)
    spool.initial_weight_g = float(request.form.get('initial_weight_g', spool.initial_weight_g) or spool.initial_weight_g)
    spool.purchase_cost = float(request.form.get('purchase_cost', 0) or 0)
    spool.location = request.form.get('location', '')
    spool.notes = request.form.get('notes', '')
    db.session.commit()
    flash('Spool updated')
    return redirect(url_for('filament.index'))


@filament_bp.route('/spool/<int:spool_id>/delete', methods=['POST'])
@login_required
def delete_spool(spool_id):
    spool = FilamentSpool.query.get_or_404(spool_id)
    db.session.delete(spool)
    db.session.commit()
    flash('Spool removed')
    return redirect(url_for('filament.index'))
