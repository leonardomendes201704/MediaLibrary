from app.db.database import Base, engine


def main() -> None:
    Base.metadata.create_all(bind=engine)
    print("Database schema initialized.")


if __name__ == "__main__":
    main()

