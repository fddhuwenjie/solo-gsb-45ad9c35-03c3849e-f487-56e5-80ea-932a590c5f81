"""请求体模型。ge=1 与 ge=0/le=100 在入口即拒绝非法值（422）。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class CourseIn(BaseModel):
    credits: int = Field(..., ge=1, description="正整数学分")
    score: int = Field(..., ge=0, le=100, description="0~100 分成绩")


class RequirementIn(BaseModel):
    required_credits: int = Field(..., ge=1, description="所需学分（正整数）")


class SubstitutionIn(BaseModel):
    min_score: int = Field(..., ge=0, le=100, description="替代所需最低成绩")


class RecognitionItem(BaseModel):
    course_code: str = Field(..., min_length=1)
    requirement_code: str = Field(..., min_length=1)


class RecognitionBatchIn(BaseModel):
    items: list[RecognitionItem] = Field(..., min_length=1)


class ScoreIn(BaseModel):
    score: int = Field(..., ge=0, le=100, description="更正后的 0~100 分成绩")


class ReviewIn(BaseModel):
    pass
