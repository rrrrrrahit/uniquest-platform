"""Создание учётки admin при старте (без Render Shell)."""

from django.contrib.auth.models import User

DEFAULT_ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_PASSWORD = "admin123456"
DEFAULT_ADMIN_EMAIL = "admin@uniquest.kz"


def ensure_default_admin():
    """Создаёт или сбрасывает пароль суперпользователя admin."""
    user, _created = User.objects.get_or_create(
        username=DEFAULT_ADMIN_USERNAME,
        defaults={
            "email": DEFAULT_ADMIN_EMAIL,
            "is_staff": True,
            "is_superuser": True,
            "is_active": True,
        },
    )
    user.set_password(DEFAULT_ADMIN_PASSWORD)
    user.email = DEFAULT_ADMIN_EMAIL
    user.is_staff = True
    user.is_superuser = True
    user.is_active = True
    user.save()

    # Иначе после входа admin уходит на панель преподавателя, а не админ-дашборд.
    try:
        from .models import Profile

        Profile.objects.filter(user=user).delete()
    except Exception:
        pass

    return user
