from pydantic import BaseModel, EmailStr
from typing import Optional


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str
    display_name: str


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class LogEntryCreate(BaseModel):
    food_id: int
    quantity_g: float
    eaten_at: Optional[str] = None
    source: str = "manual"
    notes: Optional[str] = None


class WaterUpdate(BaseModel):
    glasses: int                       # absolute count to set
    date: Optional[str] = None
    tz_offset: int = 0


class GoalsUpdate(BaseModel):
    calorie_goal: Optional[int] = None
    protein_g: Optional[float] = None
    carbs_g: Optional[float] = None
    fat_g: Optional[float] = None


class DeleteAccountRequest(BaseModel):
    password: str          # re-confirmed so a stolen session can't wipe data


class PushSubscription(BaseModel):
    endpoint: str
    keys: dict


class Unsubscribe(BaseModel):
    endpoint: str


class ReminderCreate(BaseModel):
    time_local: str          # "HH:MM" in the user's local time
    tz_offset: int = 0       # JS getTimezoneOffset() at creation time


class ReminderUpdate(BaseModel):
    enabled: bool


class RecipeIngredientIn(BaseModel):
    food_id: int
    quantity_g: float


class UserFoodCreate(BaseModel):
    """A recipe (built from `ingredients`) OR a custom food (manual per-serving
    macros, when `ingredients` is empty)."""
    name: str
    serving_label: Optional[str] = None        # e.g. "bowl", "cup"
    servings: float = 1                         # how many servings the recipe yields
    ingredients: list[RecipeIngredientIn] = []
    # Manual per-serving macros (used only when `ingredients` is empty)
    calories: Optional[float] = None
    protein_g: Optional[float] = None
    carbs_g: Optional[float] = None
    fat_g: Optional[float] = None
    fiber_g: Optional[float] = None
