package com.github.rtisma.toolbox.file_renamer;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.Collection;
import java.util.List;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.regex.Pattern;

import com.google.common.base.Joiner;
import com.google.common.hash.Hashing;
import lombok.NonNull;
import lombok.RequiredArgsConstructor;
import lombok.SneakyThrows;
import lombok.extern.slf4j.Slf4j;
import lombok.val;

import static java.nio.file.Files.isDirectory;
import static java.nio.file.Files.isRegularFile;
import static java.nio.file.Files.readAllBytes;
import static java.util.Objects.nonNull;
import static java.util.stream.Collectors.toUnmodifiableList;
import static lombok.Lombok.sneakyThrow;

@Slf4j
@RequiredArgsConstructor
public class Renamer implements AutoCloseable
{

  private static final Pattern PATTERN = Pattern.compile("^(.*)\\.([^\\.]+)$");

  @NonNull private final ExecutorService executor;

  @SneakyThrows
  public void rename(@NonNull final List<Path> rootPaths, @NonNull final String matchPattern, final boolean recursive) {
    val futures = new ArrayList<Future<?>>();
    rename2(futures, rootPaths, matchPattern, recursive);
    waitForFutures(futures);
    log.info("doneeeeeeeeeeeee");
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
          .forEach(x -> executor.submit(() -> rename(x)));
    }
    log.warn("The following were missing paths: [{}]", Joiner.on(", ").join(missing));
  }

  @Override
  public void close() throws Exception {
    if (nonNull(executor)) {
      executor.shutdown();
      executor.awaitTermination(10, TimeUnit.SECONDS);
    }
  }

  @SneakyThrows
  private static String getMd5(Path file) {
    return Hashing.md5()
        .hashBytes(readAllBytes(file))
        .toString();
  }


  private static Path appendMd5String(Path file, String md5) {
    val matcher = PATTERN.matcher(file.toString());
    String out;
    if (matcher.matches()) {
      out = matcher.group(1)+"__md5-"+md5+"."+matcher.group(2);
    } else {
      out = file+"-"+md5;
    }
    return Paths.get(out);
  }

  //TODO: check if it was already renamed
  @SneakyThrows
  private static void rename(Path file) {
    val newFile = appendMd5String(file, getMd5(file));
    file.toFile().renameTo(newFile.toFile());
    log.info("{} ---> {}", file, newFile);
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

  public static Renamer createRenamer(final int numThreads) {
    return new Renamer(Executors.newFixedThreadPool(numThreads));
  }
}
