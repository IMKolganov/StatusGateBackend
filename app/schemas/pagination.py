from pydantic import BaseModel, create_model


def paginated_of(item_model: type[BaseModel]) -> type[BaseModel]:
    return create_model(
        f"Paginated{item_model.__name__}",
        items=(list[item_model], ...),
        total=(int, ...),
        offset=(int, ...),
        limit=(int, ...),
        has_next=(bool, ...),
        has_previous=(bool, ...),
        __base__=BaseModel,
    )
