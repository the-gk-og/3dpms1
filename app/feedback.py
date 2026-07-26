from flask import Blueprint, request
from flask_login import login_required

from app.models import FeedbackSurvey
from app.helpers import render_template

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
    )


@feedback_bp.route('/<int:survey_id>')
@login_required
def detail(survey_id):
    survey = FeedbackSurvey.query.get_or_404(survey_id)
    return render_template('feedback_detail.html', survey=survey)
