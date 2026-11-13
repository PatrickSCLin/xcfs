"""The persistent build session for the Xcode adapter (S3). Until it is enabled, every build uses xcodebuild."""


def choose_driver(adapter, ctx):
    return "xcodebuild"


def invalidate(ctx):
    pass


def stop(ctx):
    pass


def doctor(adapter, root, entry):
    return []
