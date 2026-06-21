from sqlalchemy import Column, Integer, String, Float, Boolean, Date, ForeignKey, DateTime, Text
from sqlalchemy.orm import relationship
from datetime import datetime
from app.database import Base


class Trial(Base):
    __tablename__ = "trials"
    id = Column(Integer, primary_key=True, index=True)
    year = Column(Integer, nullable=False)
    crop_name = Column(String(100), nullable=False)
    status = Column(String(20), default="进行中")
    created_at = Column(DateTime, default=datetime.utcnow)

    varieties = relationship("Variety", back_populates="trial", cascade="all, delete-orphan")
    sites = relationship("Site", back_populates="trial", cascade="all, delete-orphan")
    plots = relationship("Plot", back_populates="trial", cascade="all, delete-orphan")


class Variety(Base):
    __tablename__ = "varieties"
    id = Column(Integer, primary_key=True, index=True)
    trial_id = Column(Integer, ForeignKey("trials.id"), nullable=False)
    code = Column(String(20), nullable=False)
    name = Column(String(100), nullable=False)
    is_control = Column(Boolean, default=False)

    trial = relationship("Trial", back_populates="varieties")
    plots = relationship("Plot", back_populates="variety")


class Site(Base):
    __tablename__ = "sites"
    id = Column(Integer, primary_key=True, index=True)
    trial_id = Column(Integer, ForeignKey("trials.id"), nullable=False)
    code = Column(String(20), nullable=False)
    name = Column(String(100), nullable=False)
    latitude = Column(Float, nullable=False)
    longitude = Column(Float, nullable=False)
    altitude = Column(Float, nullable=False)
    soil_type = Column(String(50), nullable=False)

    trial = relationship("Trial", back_populates="sites")
    plots = relationship("Plot", back_populates="site")


class Plot(Base):
    __tablename__ = "plots"
    id = Column(Integer, primary_key=True, index=True)
    trial_id = Column(Integer, ForeignKey("trials.id"), nullable=False)
    site_id = Column(Integer, ForeignKey("sites.id"), nullable=False)
    variety_id = Column(Integer, ForeignKey("varieties.id"), nullable=False)
    plot_code = Column(String(50), nullable=False, unique=True)
    replication = Column(Integer, nullable=False)

    trial = relationship("Trial", back_populates="plots")
    site = relationship("Site", back_populates="plots")
    variety = relationship("Variety", back_populates="plots")
    phenology = relationship("PhenologyData", back_populates="plot", uselist=False, cascade="all, delete-orphan")
    yield_data = relationship("YieldData", back_populates="plot", uselist=False, cascade="all, delete-orphan")


class PhenologyData(Base):
    __tablename__ = "phenology_data"
    id = Column(Integer, primary_key=True, index=True)
    plot_id = Column(Integer, ForeignKey("plots.id"), nullable=False, unique=True)
    sowing_date = Column(Date, nullable=True)
    emergence_date = Column(Date, nullable=True)
    heading_date = Column(Date, nullable=True)
    maturity_date = Column(Date, nullable=True)

    plot = relationship("Plot", back_populates="phenology")


class YieldData(Base):
    __tablename__ = "yield_data"
    id = Column(Integer, primary_key=True, index=True)
    plot_id = Column(Integer, ForeignKey("plots.id"), nullable=False, unique=True)
    plant_height = Column(Float, nullable=True)
    grains_per_spike = Column(Integer, nullable=True)
    thousand_grain_weight = Column(Float, nullable=True)
    plot_yield = Column(Float, nullable=True)

    plot = relationship("Plot", back_populates="yield_data")
