# import os

# from django.core.asgi import get_asgi_application
# from channels.auth import AuthMiddlewareStack
# from channels.routing import ProtocolTypeRouter, URLRouter
# import myapp.routing

# os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'mysite.settings')

# application = ProtocolTypeRouter({
#     'http':get_asgi_application(),
#     'websocket':AuthMiddlewareStack(
#         URLRouter(myapp.routing.websocket_urlpatterns)
#         ),
#         })


import os

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'mysite.settings')

from django.core.asgi import get_asgi_application
# get_asgi_application() runs django.setup(), which must happen BEFORE
# myapp.routing (and everything it imports, like models.py) gets imported.
# Importing it any earlier fails outside `manage.py runserver`, because
# nothing else has configured settings yet -- which is exactly what broke
# on Render, where daphne imports this module directly.
django_asgi_app = get_asgi_application()

from channels.auth import AuthMiddlewareStack
from channels.routing import ProtocolTypeRouter, URLRouter
import myapp.routing

application = ProtocolTypeRouter({
    'http': django_asgi_app,
    'websocket': AuthMiddlewareStack(
        URLRouter(myapp.routing.websocket_urlpatterns)
    ),
})