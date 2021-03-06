from datetime import datetime
from time import time

from app import db, login
from werkzeug.security import generate_password_hash, check_password_hash
from flask_login import UserMixin
from flask import current_app, url_for

from hashlib import md5
from itsdangerous import TimedJSONWebSignatureSerializer as Serializer
from itsdangerous import BadSignature, SignatureExpired

import jwt
from app.search import add_to_index, remove_from_index, query_index, remove_all

import json

import redis
import rq

import base64
from datetime import datetime, timedelta
import os

# 用户粉丝，使用多对多关系，并且是自引用关系，以下建立一个关联表
followers = db.Table('followers',
                     db.Column('follower_id', db.Integer, db.ForeignKey('user.id')),
                     db.Column('followed_id', db.Integer, db.ForeignKey('user.id')),
                     )


# 一个API接口的分页组织结构，用来组织和用户集合类型有关的信息，在User中继承
# 之所以做一个基类，是因为返回的用户集合类型的请求有多种，比如请求所有用户、
# 请求某个用户的所有粉丝、请求某个用户的所有关注等
# 做一个基类，可以一劳永逸
class PaginatedAPIMixin(object):
    @staticmethod
    # 生成用户信息资源的组织
    def to_collection_dict(query, page, per_page, endpoint, **kwargs):
        resources = query.paginate(page, per_page, False)
        data = {
            'items': [item.to_dict() for item in resources.items],
            '_meta': {
                'page': page,
                'per_page': per_page,
                'total_pages': resources.pages,
                'total_items': resources.total
            },
            # 超媒体资源链接
            '_links': {
                # 为了保证通用性，从endpoint中获取需要发送到url_for的视图函数
                # **kwargs是发送到路由的其他参数
                'self': url_for(endpoint, page=page, per_page=per_page,
                                **kwargs),
                'next': url_for(endpoint, page=page + 1, per_page=per_page,
                                **kwargs) if resources.has_next else None,
                'prev': url_for(endpoint, page=page - 1, per_page=per_page,
                                **kwargs) if resources.has_prev else None
            }
        }
        return data


class User(PaginatedAPIMixin, UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), index=True, unique=True)
    email = db.Column(db.String(128), index=True, unique=True)
    password_hash = db.Column(db.String(128))

    # 表间的高级映射，这里是一对多关系，user是一，post是多
    posts = db.relationship('Post', backref='author', lazy='dynamic')

    about_me = db.Column(db.String(140))
    last_seen = db.Column(db.DateTime, default=datetime.utcnow)

    # 支持私有消息
    messages_sent = db.relationship('Message', foreign_keys='Message.sender_id',
                                    backref='author', lazy='dynamic')
    messages_received = db.relationship('Message', foreign_keys='Message.recipient_id',
                                        backref='recipient', lazy='dynamic')
    last_message_read_time = db.Column(db.DateTime)

    # 私有消息通知
    notifications = db.RelationshipProperty('Notification',
                                            backref='user', lazy='dynamic')

    # 粉丝
    followed = db.relationship(
        'User', # 关联表右侧实体，是被关注的一方，左侧实体，即关注者，是其上级实体
        secondary=followers,    # 指定用于该关系的关联表
        primaryjoin=(followers.c.follower_id == id),    # 通过关联表关联到左侧实体（关注者）的条件
        secondaryjoin=(followers.c.followed_id == id),  # 通过关联表关联到右侧实体（被关注者）的条件
        backref=db.backref('followers', lazy='dynamic'), lazy='dynamic' # 指定右侧实体如何访问关系
    )

    # 后台任务
    tasks = db.relationship('Task', backref='user', lazy='dynamic')

    # 用于API用户验证的token字段
    # token
    token = db.Column(db.String(32), index=True, unique=True)
    # token过期时间
    token_expiration = db.Column(db.DateTime)

    # 调试时打印输出
    def __repr__(self):
        return '<User {}>'.format(self.username)

    # 计算明文密码的hash密码
    def set_password(self, password):
        self.password_hash = generate_password_hash(password=password)

    # 校验明文密码与hash密码是否一致的辅助函数
    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    # 该函数可以通过传入的邮件名称来从gravatar获取一个头像
    def avatar(self, size):
        digest = md5(self.email.lower().encode('utf-8')).hexdigest()
        return 'https://www.gravatar.com/avatar/{}?d=identicon&s={}'.format(digest, size)

    # 为API授权登陆提供的TOKEN生成接口
    def generate_auth_token(self, expiration: object = 600) -> object:
        s = Serializer(current_app.config['SECRET_KEY'], expires_in=expiration)
        return s.dumps({ 'id': self.id })

    # 为API授权登陆提供的TOKEN验证接口
    @staticmethod
    def verify_auth_token(token):
        s = Serializer(current_app.config['SECRET_KEY'])
        try:
            data = s.loads(token)
        except SignatureExpired:
            return None # 验证成功，但token已超时过期
        except BadSignature:
            return None # 验证失败

        # 验证成功，获取用户信息
        user = User.query.get(data['id'])
        return user

    # 关注用户
    def follow(self, user):
        if not self.is_following(user):
            self.followed.append(user)

    # 取消关注用户
    def unfollow(self, user):
        if self.is_following(user):
            self.followed.remove(user)

    # 检查是否已关注
    def is_following(self, user):
        return self.followed.filter(
            followers.c.followed_id == user.id).count() > 0 # 这里使用count是教学需要，实际上只会返回0或1

    # 查询所有关注用户的动态，并按条件返回信息
    def followed_posts(self):
        # 条件查询操作+条件过滤，查找关注的用户的动态并排序
        followed = Post.query.join(
            followers, (followers.c.followed_id == Post.user_id)).filter(
                followers.c.follower_id == self.id)
        # 查找自己的动态
        own = Post.query.filter_by(user_id=self.id)
        # 将两者结合起来，并排序
        return followed.union(own).order_by(Post.timestamp.desc())

    # 获取重置密码的token，token过期时间是10分钟
    def get_reset_password_token(self, expires_in=600):
        # 加密秘钥是config中的SECRET_KEY，token的字段由一个用户id和一个过期时间组成
        # 注意到，这个encode中的参数algorithm不带s
        # utf-8是必须的，因为encode会按照字节序列返回，而在应用中，使用字符串更方便
        return jwt.encode({'reset_password': self.id, 'exp': time() + expires_in},
                        current_app.config['SECRET_KEY'], algorithm='HS256').decode('utf-8')

    # 验证重置密码的token
    @staticmethod
    def verify_reset_password_token(token):
        try:
            # 注意到，这个decode中的参数algorithms带s
            # decode验证不通过会触发异常，所以放到try中
            id = jwt.decode(token, current_app.config['SECRET_KEY'], algorithms=['HS256'])['reset_password']
        except:
            return
        # 获取到提取id对应的用户
        return User.query.get(id)

    # 私有消息功能辅助函数，返回用户有多少条未读私有消息
    def new_messages(self):
        last_read_time = self.last_message_read_time or datetime(1900, 1, 1)
        return Message.query.filter_by(recipient=self).filter(Message.timestamp > last_read_time).count()

    # 接收私有消息的辅助函数
    def add_notification(self, name, data):
        self.notifications.filter_by(name=name).delete()
        n = Notification(name=name, payload_json=json.dumps(data), user=self)
        db.session.add(n)
        return n

    # 后台作业的辅助方法
    # 将任务提交到队列，并添加到数据库
    def launch_task(self, name, description, *args, **kwargs):
        rq_job = current_app.task_queue.enqueue('app.tasks.' + name, self.id, *args, **kwargs)
        task = Task(id=rq_job.get_id(), name=name, description=description, user=self)
        # 将rq_job id作为主键的task对象添加到数据库中
        db.session.add(task)
        # 没有commit，将在高层次的函数中提交
        return task

    # 后台作业的辅助方法
    # 返回所有未结束的任务
    def get_tasks_in_progress(self):
        return Task.query.filter_by(user=self, complete=False).all()

    # 后台作业的辅助方法
    # 返回某个指定的仍在运行的任务
    def get_task_in_progress(self, name):
        return Task.query.filter_by(name=name, user=self, complete=False).first()

    # 将User模型转换成字典格式的表示形式，用于api接口返回用户信息（将会进一步转换为json格式）
    def to_dict(self, include_email=False):
        data = {
            'id': self.id,
            'username': self.username,
            # 使用ISO 8601时间格式，通过isoformat方法生成, Z符号指定是UTC时区代码
            'last_seen': self.last_seen.isoformat() + 'Z',
            'about_me': self.about_me,
            'post_count': self.posts.count(),
            'follower_count': self.followers.count(),
            'followed_count': self.followed.count(),
            # _links字段是为了满足纯粹Rest API标准的超媒体要求，添加所有可能的资源访问链接
            # 超媒体的好处是，客户端不需要记住很多的API请求URL，只需要从每个API请求响应中的
            # 超媒体段（_links）中获得与这个响应内容相关的API请求URL
            '_links': {
                'self': url_for('api.get_user', id=self.id),
                'followers': url_for('api.get_followers', id=self.id),
                'followed': url_for('api.get_followed', id=self.id),
                # 头像资源所对应的URL依然是通过avatar方法访问的avatar url
                'avatar': self.avatar(128)
            }
        }
        # email地址默认不会返回，除非用户主动指定返回，所以任何用户只能查看自己的email地址
        if include_email:
            data['email'] = self.email
        return data

    # 与to_dict相反的功能，将字典格式的用户信息转换为模型
    def from_dict(self, data, new_user=False):
        for field in ['username', 'email', 'about_me']:
            if field in data:
                setattr(self, field, data[field])
            # 如果是新用户的话，返回信息中还会带有密码，用来更新用户的初始密码
            if new_user and 'password' in data:
                self.set_password(data['password'])

    # 获取token或更新token
    def get_token(self, expires_in=3600):
        now = datetime.utcnow()
        # 如果已经有token并且到期时间距离当前时间还有大于1分钟的时间，则仍返回当前的token
        if self.token and self.token_expiration > now + timedelta(seconds=60):
            return self.token
        # 以base64编码的24位随机字符串来生成token
        # 保证token中所有字符都处于可读字符串范围内
        self.token = base64.b64encode(os.urandom(24)).decode('utf-8')
        self.token_expiration = now + timedelta(seconds=expires_in)
        db.session.add(self)
        return self.token

    # 使token失效，修改过期时间为当前时间减1秒
    # 这是一种好的安全策略
    def revoke_token(self):
        self.token_expiration = datetime.utcnow() - timedelta(seconds=1)

    # 该方法传入token值并检查该token所属的用户，如果token无效或过期，则返回None
    @staticmethod
    def check_token(token):
        user = User.query.filter_by(token=token).first()
        if user is None or user.token_expiration < datetime.utcnow():
            return None
        return user


# 为了保证数据库变化时自动触发elasticsearch索引动作，而设计的一个mixin类
# 从而通过SQLAlchemy的事件机制来触发elasticsearch的动作
# app/search.py中的方法不能外部调用，因为可能会导致数据库和搜索索引内容不一致
# 这些方法在mixin类中被同步调用
class SearchableMixin(object):
    @classmethod
    def search(cls, expression, page, per_page):
        # 返回结果ID列表和总数
        ids, total = query_index(cls.__tablename__, expression, page, per_page)
        if total == 0:
            return cls.query.filter_by(id=0), 0
        when = []
        for i in range(len(ids)):
            when.append((ids[i], i))
        return cls.query.filter(cls.id.in_(ids)).order_by(
            # 使用case语句，确保数据库中的结果与给定ID的顺序相同
            # 因为使用elasticsearch搜索返回的结果不是有序的
            db.case(when, value=cls.id)), total

    # 在before_commit中保存需要处理的数据，这个事件是在SQLAlchemy提交操作之前触发
    # 在SQLAlchemy提交之后，将需要保存的数据更新elasticsearch
    # 这么做的原因就是SQLAlchemy可能提交失败，对数据的完整性有保障
    @classmethod
    def before_commit(cls, session):
        session._changes = {
            'add': [obj for obj in session.new if isinstance(obj, cls)],
            'update': [obj for obj in session.dirty if isinstance(obj, cls)],
            'delete': [obj for obj in session.deleted if isinstance(obj, cls)]
        }

    # SQLAlchemy提交之后，将before_commit中保存的数据更新elasticsearch
    @classmethod
    def after_commit(cls, session):
        for obj in session._changes['add']:
            add_to_index(cls.__tablename__, obj)
        for obj in session._changes['update']:
            add_to_index(cls.__tablename__, obj)
        for obj in session._changes['delete']:
            remove_from_index(cls.__tablename__, obj)
        session._changes = None

    # 一个辅助函数，能够将表中所有的内容添加到搜索索引中
    # 通过执行Post.reindex()即可
    @classmethod
    def reindex(cls):
        for obj in cls.query:
            print("Add Post: <" + str(obj.id) + " " + obj.body + "> into " + cls.__tablename__)
            add_to_index(cls.__tablename__, obj)

    # 一个辅助函数，能够删除该表的所有索引
    @classmethod
    def delete_index(cls):
        remove_all(cls.__tablename__)


# 第一个继承的类，用于指定Post表更新时的触发事件
class Post(SearchableMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    body = db.Column(db.String(140))
    timestamp = db.Column(db.DateTime, index=True, default=datetime.utcnow)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))

    language = db.Column(db.String(5))

    # 用于使能搜索的标志
    # 指定搜索的字段是body
    __searchable__ = ['body']

    def  __repr__(self):
        return '<Post {}>'.format(self.body)


# 注册Post的事件处理函数
db.event.listen(db.session, 'before_commit', Post.before_commit)
db.event.listen(db.session, 'after_commit', Post.after_commit)


# 为Flask-Login准备的辅助加载用户的函数
@login.user_loader
def load_user(id):
    return User.query.get(int(id))


# 支持私有消息功能
class Message(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    # 发件人
    sender_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    # 收件人
    recipient_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    body = db.Column(db.String(140))
    # 最后一次阅读私有消息的时间
    timestamp = db.Column(db.DateTime, index=True, default=datetime.utcnow())

    def __repr(self):
        return '<Message {}>'.format(self.body)

# 私有消息的通知
class Notification(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    timestamp = db.Column(db.Float, index=True, default=time)
    payload_json = db.Column(db.Text)

    def get_data(self):
        return json.loads(str(self.payload_json))

# 维护后台任务的数据表
class Task(db.Model):
    # 这里的id的类型是string，这是因为我们将以任务id作为主键，而不是默认主键
    id = db.Column(db.String(36), primary_key=True)
    name = db.Column(db.String(128), index=True)
    description = db.Column(db.String(128))
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    complete = db.Column(db.Boolean, default=False)

    def get_rq_job(self):
        try:
            rq_job = rq.job.Job.fetch(self.id, connection=current_app.redis)
        except (redis.exceptions.RedisError, rq.exceptions.NoSuchJobError):
            return None
        return rq_job

    # 获取job进度
    def get_progress(self):
        job = self.get_rq_job()
        # 如果job不存在，则返回100，如果job中没有progress属性，则返回0
        return job.meta.get('progress', 0) if job is not None else 100


