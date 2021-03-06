from flask import request
from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, BooleanField, SubmitField, TextAreaField# 引入表单字段
from wtforms.validators import DataRequired, ValidationError, Email, EqualTo, Length
from flask_babel import _, lazy_gettext as _l
from app.models import User

class EditProfileForm(FlaskForm):
    username = StringField(_l('Username'), validators=[DataRequired()])
    about_me = TextAreaField(_l('About me'), validators=[Length(min=0, max=140)])
    submit = SubmitField(_l('Submit'))

    # 自定义构造函数，增加一个original_username的参数
    def __init__(self, original_username, *args, **kwargs):
        super(EditProfileForm, self).__init__(*args, **kwargs)
        self.original_username = original_username

    def validate_username(self, username):
        # 如果表单中的用户名和原始用户名相同，表示用户修改用户名没效果，但不会因为检查到数据库中自己的名字而报错用户名重复，所以加一个判断
        if username.data != self.original_username:
            user = User.query.filter_by(username=self.username.data).first()
            if user is not None:
                raise ValidationError(_l('Please user a different username.'))


class PostForm(FlaskForm):
    post = TextAreaField(_l('Say something'), validators=[DataRequired(), Length(min=1, max=140)])
    submit = SubmitField(_l('Submit'))

class SearchForm(FlaskForm):
    q = StringField(_l('Search'), validators=[DataRequired()])

    # 通过GET请求的表单，所以不需要submit

    def __init__(self, *args, **kwargs):
        # 需要指定GET请求提交的表单在查询字符串中传递字段值
        if 'formdata' not in kwargs:
            kwargs['formdata'] = request.args

        # 需要指定禁用CSRF保护，因为是GET请求
        if 'csrf_enabled' not in kwargs:
            kwargs['csrf_enabled'] = False
        super(SearchForm, self).__init__(*args, **kwargs)

# 私有消息表单
class MessageForm(FlaskForm):
    message = TextAreaField(_l('Message'), validators=[
        DataRequired(), Length(min=0, max=140)])
    submit = SubmitField(_l('Submit'))

