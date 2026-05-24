from django import forms
from django.contrib.auth.models import User
from django.contrib.auth.forms import UserCreationForm
from .models import Profile, Specialty, Grade, Enrollment, Course, Lecture, Group, Student


def _extract_lecture_text(uploaded_file):
    """
    Извлекает текст из загруженного файла лекции.
    Поддерживаются txt/md/csv/json, а также pdf/docx при наличии зависимостей.
    """
    if not uploaded_file:
        return ""

    name = (uploaded_file.name or "").lower()
    ext = name.rsplit(".", 1)[-1] if "." in name else ""
    raw = uploaded_file.read()

    # Текстовые форматы.
    if ext in {"txt", "md", "csv", "json", "log", "py"}:
        for encoding in ("utf-8", "utf-8-sig", "cp1251", "latin1"):
            try:
                return raw.decode(encoding)
            except Exception:
                continue
        return ""

    # PDF (опционально).
    if ext == "pdf":
        try:
            from pypdf import PdfReader  # type: ignore
            import io

            reader = PdfReader(io.BytesIO(raw))
            pages = []
            for page in reader.pages:
                pages.append(page.extract_text() or "")
            return "\n".join(pages).strip()
        except Exception:
            return ""

    # DOCX (опционально).
    if ext == "docx":
        try:
            from docx import Document  # type: ignore
            import io

            doc = Document(io.BytesIO(raw))
            return "\n".join(p.text for p in doc.paragraphs if p.text).strip()
        except Exception:
            return ""

    return ""

# ----------------- Регистрация пользователя -----------------
class UserRegisterForm(UserCreationForm):
    email = forms.EmailField(required=True, label='Email')
    first_name = forms.CharField(required=False, label='Имя')
    last_name = forms.CharField(required=False, label='Фамилия')
    role = forms.ChoiceField(choices=Profile.ROLE_CHOICES, required=True, label='Роль')
    group = forms.ModelChoiceField(
        queryset=Group.objects.all(),
        required=False,
        label='Группа',
        help_text='Учебная группа (для студентов)'
    )
    specialty = forms.ModelChoiceField(
        queryset=Specialty.objects.all(),
        required=False,
        label='Специальность',
        help_text='Выберите специальность (для студентов)'
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['group'].queryset = Group.objects.all().order_by('-year', 'name')
        self.fields['specialty'].queryset = Specialty.objects.all().order_by('code', 'name_ru')
        self.fields['group'].empty_label = '---------'
        self.fields['specialty'].empty_label = '---------'

    class Meta:
        model = User
        fields = ['username', 'first_name', 'last_name', 'email', 'role', 'group', 'specialty', 'password1', 'password2']
        labels = {'username': 'Логин'}

    def clean(self):
        cleaned_data = super().clean()
        role = cleaned_data.get('role')
        group = cleaned_data.get('group')
        specialty = cleaned_data.get('specialty')

        if role == Profile.ROLE_STUDENT:
            if not Group.objects.exists():
                self.add_error('group', 'В системе пока нет учебных групп. Обратитесь к администратору.')
            if not Specialty.objects.exists():
                self.add_error('specialty', 'В системе пока нет специальностей. Обратитесь к администратору.')
            if not group:
                self.add_error('group', 'Для студента необходимо указать учебную группу.')
            if not specialty:
                self.add_error('specialty', 'Для студента необходимо указать специальность.')
        elif role == Profile.ROLE_TEACHER:
            # For teachers, these fields must stay empty.
            cleaned_data['group'] = None
            cleaned_data['specialty'] = None

        return cleaned_data

# ----------------- Обновление пользователя -----------------
class UserUpdateForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ['first_name', 'last_name', 'email']
        labels = {
            'first_name': 'Имя',
            'last_name': 'Фамилия',
            'email': 'Email',
        }

# ----------------- Обновление профиля -----------------
class ProfileUpdateForm(forms.ModelForm):
    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user

        role = None
        if user and hasattr(user, 'profile'):
            role = user.profile.role

        # Student-specific fields are hidden for teachers.
        if role == Profile.ROLE_TEACHER:
            self.fields.pop('group', None)
            self.fields.pop('specialty', None)

    def save(self, commit=True):
        profile = super().save(commit=False)
        role = getattr(profile, 'role', None)
        if role == Profile.ROLE_TEACHER:
            profile.group = None
            profile.specialty = None
        if commit:
            profile.save()
        return profile

    class Meta:
        model = Profile
        fields = ['photo', 'bio', 'phone', 'group', 'specialty', 'address', 'iin']
        widgets = {
            'bio': forms.Textarea(attrs={'rows':3, 'class': 'form-control'}),
            'phone': forms.TextInput(attrs={'class': 'form-control'}),
            'address': forms.Textarea(attrs={'rows':2, 'class': 'form-control'}),
            'iin': forms.TextInput(attrs={'class': 'form-control'}),
        }
        labels = {
            'photo': 'Фото',
            'bio': 'Биография',
            'phone': 'Телефон',
            'group': 'Группа',
            'specialty': 'Специальность',
            'address': 'Адрес',
            'iin': 'ИИН',
        }

# ----------------- Форма выставления оценки преподавателем -----------------
class TeacherGradeForm(forms.ModelForm):
    class Meta:
        model = Grade
        fields = ['enrollment', 'assignment', 'value', 'topic', 'comment']
        labels = {
            'enrollment': 'Студент и курс',
            'assignment': 'Задание',
            'value': 'Балл',
            'topic': 'Тема',
            'comment': 'Комментарий',
        }
        widgets = {'comment': forms.Textarea(attrs={'rows': 2})}

    def __init__(self, *args, teacher=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.teacher = teacher
        qs = Enrollment.objects.select_related('student__user', 'course')
        if teacher:
            qs = qs.filter(course__teacher=teacher)
        self.fields['enrollment'].queryset = qs
        self.fields['enrollment'].label_from_instance = lambda enr: f"{enr.student.last_name} {enr.student.first_name} — {enr.course.name}"

    def clean_enrollment(self):
        enrollment = self.cleaned_data.get('enrollment')
        if self.teacher and enrollment.course.teacher != self.teacher:
            raise forms.ValidationError('Можно выбирать только собственных студентов.')
        return enrollment

# ----------------- Создание лекции/ресурса -----------------
class LectureCreateForm(forms.ModelForm):
    class Meta:
        model = Lecture
        fields = ['course', 'title', 'content_text', 'content_url', 'lecture_file']
        labels = {
            'course': 'Курс',
            'title': 'Название лекции/ресурса',
            'content_text': 'Содержание',
            'content_url': 'Ссылка (необязательно)',
            'lecture_file': 'Файл лекции (необязательно)',
        }
        widgets = {
            'content_text': forms.Textarea(attrs={'rows': 4}),
            'lecture_file': forms.ClearableFileInput(attrs={'accept': '.txt,.md,.csv,.json,.pdf,.docx'}),
        }

    def __init__(self, *args, teacher=None, **kwargs):
        super().__init__(*args, **kwargs)
        qs = Course.objects.all()
        if teacher:
            qs = qs.filter(teacher=teacher)
        self.fields['course'].queryset = qs

    def clean(self):
        cleaned_data = super().clean()
        content_text = (cleaned_data.get("content_text") or "").strip()
        content_url = (cleaned_data.get("content_url") or "").strip()
        lecture_file = cleaned_data.get("lecture_file")
        if not content_text and not content_url and not lecture_file:
            raise forms.ValidationError(
                "Добавьте текст, ссылку или файл лекции."
            )
        return cleaned_data

    def save(self, commit=True):
        lecture = super().save(commit=False)
        uploaded_file = self.cleaned_data.get("lecture_file")
        if uploaded_file:
            extracted = _extract_lecture_text(uploaded_file).strip()
            uploaded_file.seek(0)
            if extracted:
                base_text = (lecture.content_text or "").strip()
                lecture.content_text = (
                    f"{base_text}\n\n=== Текст из файла {uploaded_file.name} ===\n{extracted}"
                    if base_text
                    else extracted
                )
            elif (uploaded_file.name or "").lower().endswith(".pdf"):
                raise forms.ValidationError(
                    "Не удалось извлечь текст из PDF. Добавьте описание в поле «Содержание» "
                    "или загрузите PDF с текстовым слоем (не скан без OCR)."
                )
        if commit:
            lecture.save()
        return lecture


class TeacherGradeEntryForm(forms.Form):
    course_year = forms.ChoiceField(
        required=False,
        label="Курс обучения",
        choices=[("", "Все")] + [(str(i), f"{i} курс") for i in range(1, 5)],
    )
    group = forms.ModelChoiceField(
        queryset=Group.objects.none(),
        required=False,
        label="Группа",
    )
    student = forms.ModelChoiceField(queryset=Student.objects.none(), required=False, label="Студент")
    course = forms.ModelChoiceField(
        queryset=Course.objects.none(),
        required=False,
        label="Дисциплина",
    )
    date = forms.DateField(
        label="Дата",
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    value = forms.DecimalField(label="Балл", min_value=0, max_value=100, decimal_places=2, max_digits=5)
    topic = forms.CharField(required=False, label="Тема")
    comment = forms.CharField(required=False, label="Комментарий", widget=forms.Textarea(attrs={"rows": 2}))

    def __init__(self, *args, teacher=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.teacher = teacher
        if not teacher:
            return
        groups_qs = Group.objects.filter(
            student_group__enrollments__course__teacher=teacher
        ).distinct().order_by("course_year", "name")
        courses_qs = Course.objects.filter(teacher=teacher).order_by("name")
        students_qs = Student.objects.filter(
            enrollments__course__teacher=teacher
        ).distinct().order_by("last_name", "first_name")

        selected_year = self.data.get("course_year") or self.initial.get("course_year")
        selected_group = self.data.get("group") or self.initial.get("group")

        if selected_year and str(selected_year).isdigit():
            groups_qs = groups_qs.filter(course_year=int(selected_year))
            students_qs = students_qs.filter(group__course_year=int(selected_year))

        if selected_group and str(selected_group).isdigit():
            students_qs = students_qs.filter(group_id=int(selected_group))
            courses_qs = courses_qs.filter(enrollments__student__group_id=int(selected_group)).distinct()

        self.fields["group"].queryset = groups_qs
        self.fields["course"].queryset = courses_qs
        self.fields["student"].queryset = students_qs

    def clean(self):
        cleaned = super().clean()
        student = cleaned.get("student")
        course = cleaned.get("course")
        if not student:
            self.add_error("student", "Выберите студента.")
        if not course:
            self.add_error("course", "Выберите дисциплину.")
        if student and course:
            if not Enrollment.objects.filter(student=student, course=course).exists():
                self.add_error("course", "Студент не записан на выбранную дисциплину.")
        return cleaned
