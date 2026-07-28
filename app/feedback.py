import io

import qrcode
import qrcode.image.svg
from flask import Blueprint, request, redirect, url_for, flash, Response
from flask_login import login_required

from app import db
from app.models import FeedbackSurvey
from app.helpers import render_template, log_audit, send_feedback_link_email, EmailNotConfiguredError

feedback_bp = Blueprint('feedback', __name__, url_prefix='/dash/feedback')


@feedback_bp.route('/')
@login_required
def dashboard():
    surveys = FeedbackSurvey.query.order_by(FeedbackSurvey.sent_at.desc()).all()
    responded = [s for s in surveys if s.responded]

    total_sent = len(surveys)
    total_responded = len(responded)
    response_rate = round((total_responded / total_sent) * 100) if total_sent else 0

    rated = [s.rating for s in responded if s.rating]
    avg_rating = round(sum(rated) / len(rated), 1) if rated else None

    distribution = {n: 0 for n in range(1, 6)}
    for r in rated:
        distribution[r] = distribution.get(r, 0) + 1
    max_bucket = max(distribution.values()) if rated else 0

    recommend_yes = sum(1 for s in responded if s.would_recommend is True)
    recommend_no = sum(1 for s in responded if s.would_recommend is False)

    category_fields = [
        ('Print Quality', 'print_quality_rating'),
        ('Customer Service', 'customer_service_rating'),
        ('Communication', 'communication_rating'),
        ('Turnaround Time', 'turnaround_rating'),
        ('Value for Money', 'value_rating'),
    ]
    category_averages = []
    for label, field in category_fields:
        vals = [getattr(s, field) for s in responded if getattr(s, field)]
        if vals:
            category_averages.append((label, round(sum(vals) / len(vals), 1)))

    filter_ = request.args.get('filter', 'all')
    if filter_ == 'responded':
        shown = responded
    elif filter_ == 'pending':
        shown = [s for s in surveys if not s.responded]
    else:
        shown = surveys

    return render_template(
        'feedback_dashboard.html',
        surveys=shown, filter=filter_,
        total_sent=total_sent, total_responded=total_responded, response_rate=response_rate,
        avg_rating=avg_rating, distribution=distribution, max_bucket=max_bucket,
        recommend_yes=recommend_yes, recommend_no=recommend_no,
        category_averages=category_averages,
    )


@feedback_bp.route('/<int:survey_id>')
@login_required
def detail(survey_id):
    survey = FeedbackSurvey.query.get_or_404(survey_id)
    return render_template('feedback_detail.html', survey=survey)


@feedback_bp.route('/<int:survey_id>/delete', methods=['POST'])
@login_required
def delete(survey_id):
    survey = FeedbackSurvey.query.get_or_404(survey_id)
    label = survey.job.display_number if survey.job else (survey.respondent_name or survey.client_id or 'standalone link')
    db.session.delete(survey)
    db.session.commit()
    log_audit('feedback_survey_deleted', target_type='feedback_survey', target_id=survey_id, detail=str(label))
    flash('Feedback entry deleted')
    return redirect(url_for('feedback.dashboard'))


@feedback_bp.route('/new-link', methods=['POST'])
@login_required
def new_link():
    """Creates a feedback survey with no job/quote attached \u2014 for purchases that
    never went through the system (walk-ins, marketplace sales, etc). The resulting
    link can be copied, turned into a QR code, or emailed to any address.
    """
    survey = FeedbackSurvey()
    db.session.add(survey)
    db.session.commit()
    log_audit('feedback_link_created', target_type='feedback_survey', target_id=survey.id)
    return redirect(url_for('feedback.link', survey_id=survey.id))


@feedback_bp.route('/link/<int:survey_id>')
@login_required
def link(survey_id):
    survey = FeedbackSurvey.query.get_or_404(survey_id)
    survey_url = url_for('public.feedback_survey', token=survey.token, _external=True)
    return render_template('feedback_link.html', survey=survey, survey_url=survey_url)


@feedback_bp.route('/link/<int:survey_id>/qr.svg')
@login_required
def link_qr_svg(survey_id):
    survey = FeedbackSurvey.query.get_or_404(survey_id)
    survey_url = url_for('public.feedback_survey', token=survey.token, _external=True)
    img = qrcode.make(survey_url, image_factory=qrcode.image.svg.SvgImage)
    buf = io.BytesIO()
    img.save(buf)
    return Response(buf.getvalue(), mimetype='image/svg+xml')


@feedback_bp.route('/link/<int:survey_id>/qr.png')
@login_required
def link_qr_png(survey_id):
    survey = FeedbackSurvey.query.get_or_404(survey_id)
    survey_url = url_for('public.feedback_survey', token=survey.token, _external=True)
    img = qrcode.make(survey_url)
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return Response(
        buf.getvalue(), mimetype='image/png',
        headers={'Content-Disposition': f'attachment; filename="feedback-qr-{survey.id}.png"'},
    )


@feedback_bp.route('/link/<int:survey_id>/email', methods=['POST'])
@login_required
def link_email(survey_id):
    survey = FeedbackSurvey.query.get_or_404(survey_id)
    to_email = (request.form.get('email') or '').strip()
    if not to_email:
        flash('Enter an email address to send the link to.')
        return redirect(url_for('feedback.link', survey_id=survey_id))
    try:
        send_feedback_link_email(survey, to_email)
        log_audit('feedback_link_emailed', target_type='feedback_survey', target_id=survey_id, detail=to_email)
        flash(f'Feedback link emailed to {to_email}')
    except EmailNotConfiguredError:
        flash('Email sending isn\u2019t set up yet \u2014 configure SMTP under Settings \u2192 Email.')
    except Exception:
        flash('Couldn\u2019t send the email. Please try again.')
    return redirect(url_for('feedback.link', survey_id=survey_id))
