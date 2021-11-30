package com.github.rtisma.toolbox.file_renamer;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.Collection;
import java.util.List;
import java.util.Optional;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.regex.Pattern;

import com.google.common.base.Joiner;
import com.google.common.hash.Hashing;
import lombok.Builder;
import lombok.NonNull;
import lombok.SneakyThrows;
import lombok.Value;
import lombok.extern.slf4j.Slf4j;
import lombok.val;

import static java.nio.file.Files.isDirectory;
import static java.nio.file.Files.isRegularFile;
import static java.nio.file.Files.readAllBytes;
import static java.util.Objects.nonNull;
import static java.util.concurrent.Executors.newFixedThreadPool;
import static java.util.stream.Collectors.toUnmodifiableList;
import static lombok.Lombok.sneakyThrow;

@Slf4j
public class Renamer implements AutoCloseable
{

  @NonNull private final ExecutorService executor;
  @NonNull private final String collisionPrefix;
  @NonNull private final Pattern markerPattern;
  private final boolean dryRun;
  private final boolean calcMd5;


  public Renamer(
      @NonNull final ExecutorService executor, @NonNull final String collisionPrefix,
      final boolean dryRun,
      final boolean calcMd5) {
    this.executor = executor;
    this.collisionPrefix = collisionPrefix;
    this.dryRun = dryRun;
    this.calcMd5 = calcMd5;
    this.markerPattern = Pattern.compile("^.*__"+this.collisionPrefix+"\\-[a-f0-9]{32}(\\.[^\\.]+)?$");
  }

  @SneakyThrows
  public void rename(@NonNull final List<Path> rootPaths, @NonNull final String matchPattern, final boolean recursive) {
    val futures = new ArrayList<Future<?>>();
    rename2(futures, rootPaths, matchPattern, recursive);
    waitForFutures(futures);
  }

  @SneakyThrows
  private void rename2(final List<Future<?>> jobs, @NonNull final List<Path> rootPaths, @NonNull final String matchPattern, final boolean recursive) {
    val pattern = Pattern.compile(matchPattern);

    val missing = rootPaths.stream()
        .filter(x -> !isRegularFile(x) && !isDirectory(x))
        .map(Path::toString)
        .collect(toUnmodifiableList());

    rootPaths.stream()
        .filter(Files::isRegularFile)
        .map(x -> executor.submit(() -> rename(x)))
            .forEach(jobs::add);

    if (recursive) {
      rootPaths.stream()
          .filter(Files::isDirectory)
          .flatMap(x -> {
            try {
              return Files.walk(x);
            }
            catch (IOException e) {
              e.printStackTrace();
              throw sneakyThrow(e);
            }
          })
          .filter(Files::isRegularFile)
          .filter(x -> pattern.matcher(x.getFileName().toString()).matches())
          .filter(file -> !isMarked(file))
          .forEach(x -> executor.submit(() -> rename(x)));
    }
    if (!missing.isEmpty()){
      log.warn("The following were missing paths: [{}]", Joiner.on(", ").join(missing));
    }
  }

  private boolean isMarked(Path file) {
    val result = markerPattern.matcher(file.getFileName().toString()).matches();
    if (result){
      log.warn("{} is marked", file);
    }
    return result;
  }

  @Override
  public void close() throws Exception {
    if (nonNull(executor)) {
      executor.shutdown();
      executor.awaitTermination(10, TimeUnit.SECONDS);
    }
  }

  private static String getMd5(Path file) throws IOException {
    return Hashing.md5()
        .hashBytes(readAllBytes(file))
        .toString();
  }


  @Value
  @Builder
  private static class FileNameElements {
    private static final Pattern PATTERN = Pattern.compile("^(.*)\\.([^\\.]+)$");

    String name;
    String extension;

    public static Optional<FileNameElements> parseFrom(String filename) {
      val matcher = PATTERN.matcher(filename);
      if (matcher.matches()) {
        return  Optional.of(FileNameElements.builder()
            .name(matcher.group(1))
            .extension(matcher.group(2))
            .build());
      }
      return Optional.empty();
    }
  }

  private String getMarkerPrefix() {
    return "__"+this.collisionPrefix+"-";
  }


  private String generateMd5Text(String md5) {
    return getMarkerPrefix()+md5;
  }

  private Path appendMd5String(Path file, String md5) {
    return Paths.get(FileNameElements.parseFrom(file.toString())
        .map(x -> x.getName()+generateMd5Text(md5)+"."+x.getExtension())
        .orElse(file+generateMd5Text(md5)));
  }

  @SneakyThrows
  private void rename(Path file) {
    try{
      String message = null;
      Path newFile = null;
      if (!dryRun || calcMd5) {
        newFile = appendMd5String(file, getMd5(file));
      } else {
        newFile = Paths.get(file+getMarkerPrefix()+"SOME_MD5");
      }

      message = String.format("%s ---> %s", file, newFile);
      if (dryRun) {
        log.info("[DRY-RUN] "+message);
      } else {
        file.toFile().renameTo(newFile.toFile());
        log.info(message);
      }

    } catch (IOException e) {
      log.error("Could not getMd5 for file '{}', skipping...", file);
      throw e;
    }
  }

  private static void waitForFutures(Collection<? extends Future<?>> futures) {
    futures.forEach(
        x -> {
          try {
            x.get();
          } catch (InterruptedException | ExecutionException e) {
            throw sneakyThrow(e);
          }
        });
  }

  public static Renamer createRenamer(final int numThreads, final String collisionPrefix,
                                      final boolean dryRun, final boolean calcMd5) {
    return new Renamer(newFixedThreadPool(numThreads), collisionPrefix, dryRun, calcMd5);
  }
}
