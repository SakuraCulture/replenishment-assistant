"""Identity components shared by warehouse orders and lifecycle documents."""


def warehouse_revision(head):
    """One warehouse's source signing revision, not its allocation revision."""
    return (head['oid'], head['revision'], head['warehouse'])


def expected_current_revision(head):
    """Current revision captured at selection in the head's own scope."""
    return head.get('current_revision', head['revision'])


def document_version(document):
    """Version and digest of the same document, independent of signing."""
    return (document['version'], document['digest'])
