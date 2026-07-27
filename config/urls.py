"""
URL configuration for config project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import path

from core.admin import busqueda_global_view, dashboard_view, facturar_uno_ahora_view

urlpatterns = [
    # Se registran ANTES de admin.site.urls: el panel de inicio propio de
    # Plus Digital reemplaza al dashboard genérico de Jazzmin (que solo
    # aplica si estas dos rutas no atrapan la petición primero).
    path('admin/', admin.site.admin_view(dashboard_view), name='dashboard-inicio'),
    path(
        'admin/contratos/<int:contrato_id>/facturar-ahora/',
        admin.site.admin_view(facturar_uno_ahora_view),
        name='dashboard-facturar-ahora',
    ),
    path('admin/buscar/', admin.site.admin_view(busqueda_global_view), name='dashboard-busqueda'),
    path('admin/', admin.site.urls),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
