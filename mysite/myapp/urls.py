from django.urls import path
from django.contrib.auth import views as auth_views
from . import views

urlpatterns = [
    path('signup/', views.signup, name='signup'),
    path('login/', views.PurpleLoginView.as_view(), name='login'),
    path('logout/', auth_views.LogoutView.as_view(next_page='login'), name='logout'),
    path('post-login/', views.post_login, name='post_login'),

    path('create/', views.create_room, name='create_room'),
    path('notifications/', views.notifications_view, name='notifications'),
    path('invite/<int:invite_id>/accept/', views.accept_invite, name='accept_invite'),
    path('invite/<int:invite_id>/decline/', views.decline_invite, name='decline_invite'),

    path('', views.index, name='index'),
    path('<slug:slug>/upload/', views.upload_file, name='upload_file'),
    path('<slug:slug>/', views.chatroom, name='chatroom'),
]
