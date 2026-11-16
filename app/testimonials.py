"""Customer testimonials for the public site, drawn from feedback survey responses.

A response only appears publicly when the customer ticked "OK to quote this
publicly" (testimonial_ok) AND staff chose to feature it — and it's dropped again
automatically if either of those is ever turned off.
"""
from app.models import FeedbackSurvey

# Don't advertise an average until there are enough responses for it to mean something.
MIN_REVIEWS_FOR_AVERAGE = 3


def public_testimonials(limit=6):
    rows = (FeedbackSurvey.query
            .filter(FeedbackSurvey.featured.is_(True),
                    FeedbackSurvey.testimonial_ok.is_(True),
                    FeedbackSurvey.responded_at.isnot(None),
                    FeedbackSurvey.comments.isnot(None))
            .order_by(FeedbackSurvey.responded_at.desc())
            .all())
    return [s for s in rows if (s.comments or '').strip()][:limit]


def rating_summary():
    """(average, count) over every rated response, or None if there are too few."""
    ratings = [r for (r,) in FeedbackSurvey.query
               .with_entities(FeedbackSurvey.rating)
               .filter(FeedbackSurvey.rating.isnot(None)).all()]
    if len(ratings) < MIN_REVIEWS_FOR_AVERAGE:
        return None
    return round(sum(ratings) / len(ratings), 1), len(ratings)
