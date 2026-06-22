from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.cqrs.commands.component_kinds import ComponentKindCommandHandler
from app.cqrs.commands.monitored_components import MonitoredComponentCommandHandler
from app.cqrs.commands.projects import ProjectCommandHandler
from app.cqrs.common import PaginatedResult, PaginationParams
from app.cqrs.queries.component_kinds import ComponentKindQueryHandler
from app.cqrs.queries.monitored_components import MonitoredComponentQueryHandler
from app.cqrs.queries.projects import ProjectQueryHandler
from app.models.component_kind import ComponentKind
from app.models.enums import VPN_CHECK_TYPES
from app.models.monitored_component import MonitoredComponent
from app.models.project import Project
from app.schemas.component_kind import ComponentKindCreate, ComponentKindUpdate
from app.schemas.monitored_component import MonitoredComponentCreate, MonitoredComponentUpdate
from app.schemas.project import ProjectCreate, ProjectUpdate


class ProjectService:
    def __init__(self, session: Session) -> None:
        self._queries = ProjectQueryHandler(session)
        self._commands = ProjectCommandHandler(session)

    def list(self, params: PaginationParams | None = None) -> PaginatedResult[Project]:
        return self._queries.list_paginated(params)

    def get(self, project_id: UUID) -> Project:
        project = self._queries.get_by_id(project_id)
        if project is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
        return project

    def create(self, payload: ProjectCreate) -> Project:
        if self._queries.get_by_slug(payload.slug):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Project slug already exists")
        return self._commands.create(Project(**payload.model_dump()))

    def update(self, project_id: UUID, payload: ProjectUpdate) -> Project:
        project = self.get(project_id)
        data = payload.model_dump(exclude_unset=True)
        if "slug" in data and data["slug"] != project.slug:
            existing = self._queries.get_by_slug(data["slug"])
            if existing and existing.id != project.id:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Project slug already exists")
        for key, value in data.items():
            setattr(project, key, value)
        return self._commands.update(project)

    def delete(self, project_id: UUID) -> None:
        project = self.get(project_id)
        self._commands.delete(project)
        self._commands.commit()


class ComponentKindService:
    def __init__(self, session: Session) -> None:
        self._queries = ComponentKindQueryHandler(session)
        self._commands = ComponentKindCommandHandler(session)

    def list(self, params: PaginationParams | None = None) -> PaginatedResult[ComponentKind]:
        return self._queries.list_paginated(params)

    def get(self, kind_id: UUID) -> ComponentKind:
        kind = self._queries.get_by_id(kind_id)
        if kind is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Component kind not found")
        return kind

    def create(self, payload: ComponentKindCreate) -> ComponentKind:
        if self._queries.get_by_slug(payload.slug):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Component kind slug already exists")
        return self._commands.create(ComponentKind(**payload.model_dump()))

    def update(self, kind_id: UUID, payload: ComponentKindUpdate) -> ComponentKind:
        kind = self.get(kind_id)
        data = payload.model_dump(exclude_unset=True)
        if "slug" in data and data["slug"] != kind.slug:
            existing = self._queries.get_by_slug(data["slug"])
            if existing and existing.id != kind.id:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Component kind slug already exists")
        for key, value in data.items():
            setattr(kind, key, value)
        return self._commands.update(kind)

    def delete(self, kind_id: UUID) -> None:
        kind = self.get(kind_id)
        self._commands.delete(kind)
        self._commands.commit()


class MonitoredComponentService:
    def __init__(self, session: Session) -> None:
        self._queries = MonitoredComponentQueryHandler(session)
        self._commands = MonitoredComponentCommandHandler(session)
        self._project_queries = ProjectQueryHandler(session)
        self._kind_queries = ComponentKindQueryHandler(session)

    def list(self, params: PaginationParams | None = None) -> PaginatedResult[MonitoredComponent]:
        return self._queries.list_paginated(params)

    def list_by_project(self, project_id: UUID, params: PaginationParams | None = None) -> PaginatedResult[MonitoredComponent]:
        return self._queries.list_by_project_paginated(project_id, params=params)

    def get(self, component_id: UUID) -> MonitoredComponent:
        component = self._queries.get_by_id(component_id)
        if component is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Monitored component not found")
        return component

    def create(self, payload: MonitoredComponentCreate) -> MonitoredComponent:
        if not self._project_queries.get_by_id(payload.project_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
        if not self._kind_queries.get_by_id(payload.component_kind_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Component kind not found")
        if self._queries.get_by_project_and_slug(payload.project_id, payload.slug):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Component slug already exists in project")
        return self._commands.create(MonitoredComponent(**payload.model_dump()))

    def update(self, component_id: UUID, payload: MonitoredComponentUpdate) -> MonitoredComponent:
        component = self.get(component_id)
        data = payload.model_dump(exclude_unset=True)
        effective_check_type = data.get("check_type", component.check_type)
        if data.get("speed_test_bytes") is not None and effective_check_type not in VPN_CHECK_TYPES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="speed_test_bytes is only supported for VPN check types",
            )
        project_id = data.get("project_id", component.project_id)
        slug = data.get("slug", component.slug)
        if "project_id" in data and not self._project_queries.get_by_id(data["project_id"]):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
        if "component_kind_id" in data and not self._kind_queries.get_by_id(data["component_kind_id"]):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Component kind not found")
        existing = self._queries.get_by_project_and_slug(project_id, slug)
        if existing and existing.id != component.id:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Component slug already exists in project")
        for key, value in data.items():
            setattr(component, key, value)
        return self._commands.update(component)

    def delete(self, component_id: UUID) -> None:
        component = self.get(component_id)
        self._commands.delete(component)
        self._commands.commit()
