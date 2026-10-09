from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.knowledge.service import Principal
from app.models import Course, Enrollment, User


def principal_for(db: Session, user: User) -> Principal:
    """Course access derived from enrollments (students) or all courses (admin).
    Teachers have no document access until teacher-course assignment exists (Phase 5)."""
    if user.role == "admin":
        ids = set(db.scalars(select(Course.id)))
    elif user.role == "student":
        ids = set(db.scalars(select(Enrollment.course_id).where(Enrollment.student_id == user.id)))
    else:
        ids = set()
    return Principal(user_id=user.id, role=user.role, allowed_course_ids=frozenset(ids))
