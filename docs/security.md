# Filesystem Security Assumptions

## Authorized directory trust boundary

`FilesystemService` is designed to operate only within explicitly authorized directories. Those directories must be trusted against concurrent modification by untrusted local processes.

Do not authorize a directory if an untrusted process can create, replace, rename, or remove entries inside it while Anaya is operating on those entries.

## Path and symlink protections

The service validates filenames and resolves paths against authorized roots. On supported POSIX systems, it also uses directory file descriptors and descriptor-relative operations to reduce exposure to path traversal and symbolic-link attacks.

These protections do not provide complete protection against every concurrent filesystem race, and behavior differs on platforms where descriptor-relative operations are unavailable.

## File deletion limitation

Deletion checks that the target exists and is a regular file before unlinking it. These are separate operations, not an atomic operation.

Another process with write access to the authorized directory could replace the directory entry between the check and the unlink. Consequently, the regular-file check does not guarantee that the entry removed is the same entry that was checked.

Until a stronger isolation design is implemented, the authorized-directory trust assumption is required.

## Approval is not filesystem isolation

Command confirmation and approval controls help prevent unintended actions. They do not eliminate filesystem races or prevent another local process from modifying an authorized directory concurrently.

## Deployment guidance

- Authorize only directories intended for Anaya's file operations.
- Restrict write access to those directories to trusted users and processes.
- Avoid sharing authorized directories with untrusted workloads.
- Treat stronger isolation against hostile concurrent local processes as a separate security requirement.