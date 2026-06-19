from copy import deepcopy
from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

from app.schemas.api_response import ApiErrorData
from app.schemas.auth import AccountResponse, MfaRequiredResponse


def _ensure_schema(components: dict[str, Any], model: type) -> None:
    name = model.__name__
    if name not in components:
        components[name] = model.model_json_schema(ref_template="#/components/schemas/{model}")


def _api_response_ref(name: str) -> dict[str, str]:
    return {"$ref": f"#/components/schemas/{name}"}


def _ensure_api_error_data(components: dict[str, Any]) -> None:
    components["ApiErrorData"] = ApiErrorData.model_json_schema(ref_template="#/components/schemas/{model}")


def _wrap_data_schema(components: dict[str, Any], name_hint: str, data_schema: dict[str, Any] | None) -> dict[str, str]:
    wrapped_name = f"ApiResponse_{name_hint}"
    if wrapped_name not in components:
        components[wrapped_name] = {
            "type": "object",
            "properties": {
                "success": {"type": "boolean"},
                "message": {"type": "string"},
                "data": data_schema if data_schema is not None else {"type": "null"},
            },
            "required": ["success", "message"],
        }
    return _api_response_ref(wrapped_name)


def _wrap_error_response(components: dict[str, Any]) -> dict[str, str]:
    return _wrap_data_schema(components, "Error", _api_response_ref("ApiErrorData"))


def _safe_hint(path: str, method: str, prefix: str = "") -> str:
    cleaned = path.strip("/").replace("/", "_").replace("-", "_")
    cleaned = cleaned.replace("{", "").replace("}", "")
    return f"{prefix}{method}_{cleaned}"


def _schema_name_from_ref(schema: dict[str, Any]) -> str:
    ref = schema.get("$ref", "")
    return ref.rsplit("/", 1)[-1]


def _customize_openapi_schema(schema: dict[str, Any]) -> dict[str, Any]:
    schema = deepcopy(schema)
    components = schema.setdefault("components", {}).setdefault("schemas", {})
    _ensure_api_error_data(components)
    _ensure_schema(components, AccountResponse)
    _ensure_schema(components, MfaRequiredResponse)

    login_data_schema = {
        "oneOf": [
            {"$ref": "#/components/schemas/AccountResponse"},
            {"$ref": "#/components/schemas/MfaRequiredResponse"},
        ]
    }
    manual_overrides: dict[tuple[str, str, str], dict[str, str]] = {
        ("/api/auth/login", "post", "200"): _wrap_data_schema(components, "LoginResult", login_data_schema),
        ("/api/auth/login/2fa", "post", "200"): _wrap_data_schema(
            components, "AccountResponse", {"$ref": "#/components/schemas/AccountResponse"}
        ),
        ("/api/auth/refresh", "post", "200"): _wrap_data_schema(
            components, "AccountResponse", {"$ref": "#/components/schemas/AccountResponse"}
        ),
    }

    for path, path_item in schema.get("paths", {}).items():
        for method, operation in path_item.items():
            if method not in {"get", "post", "put", "patch", "delete", "options", "head"}:
                continue

            responses = operation.get("responses", {})
            for status_code, response in list(responses.items()):
                if status_code == "204":
                    responses["200"] = {
                        "description": response.get("description", "Successful Response"),
                        "content": {
                            "application/json": {
                                "schema": _wrap_data_schema(
                                    components,
                                    _safe_hint(path, method, "Empty_"),
                                    None,
                                ),
                            }
                        },
                    }
                    del responses[status_code]
                    continue

                content = response.get("content", {})
                json_content = content.get("application/json")
                if json_content is None:
                    continue

                override_key = (path, method, status_code)
                if override_key in manual_overrides:
                    json_content["schema"] = manual_overrides[override_key]
                    continue

                if status_code.startswith("2"):
                    inner = json_content.get("schema")
                    if not inner:
                        json_content["schema"] = _wrap_data_schema(
                            components,
                            _safe_hint(path, method, "Empty_"),
                            None,
                        )
                        continue
                    hint = _schema_name_from_ref(inner) if "$ref" in inner else _safe_hint(path, method)
                    json_content["schema"] = _wrap_data_schema(components, hint, inner)
                elif status_code.startswith("4") or status_code.startswith("5"):
                    json_content["schema"] = _wrap_error_response(components)

    return schema


def setup_openapi(app: FastAPI) -> None:
    def custom_openapi() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema

        openapi_schema = get_openapi(
            title=app.title,
            version=app.version,
            routes=app.routes,
            description=app.description,
        )
        app.openapi_schema = _customize_openapi_schema(openapi_schema)
        return app.openapi_schema

    app.openapi = custom_openapi
