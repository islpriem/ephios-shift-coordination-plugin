from django.urls import include, path

from . import api, views

app_name = "ephios_shift_coordination"
planning_patterns = [
    path("planning/", views.period_list, name="period_list"),
    path("planning/new/", views.period_create, name="period_create"),
    path("planning/<int:pk>/", views.period_detail, name="period_detail"),
    path("planning/<int:pk>/open/", views.survey_open, name="survey_open"),
    path("planning/<int:pk>/close/", views.survey_close, name="survey_close"),
    path("services/<int:pk>/", views.replacement, name="replacement"),
    path("services/shifts/<int:pk>/staffing/", views.staffing_action, name="staffing_action"),
    path("planning/<int:pk>/plan/", views.plan, name="plan"),
    path("planning/<int:pk>/publication/", views.publication, name="publication"),
    path("planning/<int:pk>/publish/", views.publish, name="publish"),
    path("planning/<int:pk>/propose/", views.plan_propose, name="plan_propose"),
    path("planning/<int:pk>/validate/", views.draft_validate, name="draft_validate"),
    path("planning/<int:pk>/draft/", views.draft_save, name="draft_save"),
    path("assemblies/", views.assembly_list, name="assembly_list"),
    path("assemblies/new/", views.assembly_create, name="assembly_create"),
    path("assemblies/<int:pk>/invite/", views.assembly_invite, name="assembly_invite"),
    path("assemblies/<int:pk>/answer/", views.assembly_answer, name="assembly_answer"),
    path("assemblies/answer/<str:token>/", views.assembly_respond, name="assembly_respond"),
    path("api/now/", api.now, name="api_now"),
    path("api/next/", api.next_duty, name="api_next"),
    path("api/week/", api.week, name="api_week"),
    path("minutes/<int:pk>/file/", views.minutes_file, name="minutes_file"),
    path("minutes/<int:pk>/delete/", views.minutes_delete, name="minutes_delete"),
    path("assemblies/<int:pk>/minutes/", views.minutes_upload, name="minutes_upload"),
    path("surveys/<int:pk>/", views.survey_detail, name="survey_detail"),
    path("surveys/", views.survey_list, name="survey_list"),
    path("settings/", views.configuration, name="settings"),
    path("templates/", views.template_list, name="template_list"),
    path("templates/new/", views.template_edit, name="template_create"),
    path("templates/<int:pk>/", views.template_edit, name="template_edit"),
]

urlpatterns = [path("shift-coordination/", include(planning_patterns))]
